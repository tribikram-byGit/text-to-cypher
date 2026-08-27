import os
import re
from neo4j import GraphDatabase
from groq import Groq

# --- CONFIGURATION ---
NEO4J_URI = "neo4j+ssc://360d05b3.databases.neo4j.io"
NEO4J_AUTH = ("360d05b3", "pRBvI7D4Sd5fMVEgrCG2kh6UJrOQQhqY3kfuSTNWPek")
GROQ_API_KEY = "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"

# Initialize Groq client
client = Groq(api_key=GROQ_API_KEY)

def get_graph_schema(driver):
    """Introspects the Neo4j database dynamically."""
    schema = {"nodes": {}, "relationships": []}
    with driver.session() as session:
        labels_result = session.run("CALL db.labels()")
        for record in labels_result:
            label = record["label"]
            prop_result = session.run(f"MATCH (n:`{label}`) RETURN keys(n) AS keys LIMIT 1")
            keys = [prop_rec["keys"] for prop_rec in prop_result]
            schema["nodes"][label] = keys[0] if keys else []

        rel_result = session.run("""
            MATCH (a)-[r]->(b) 
            RETURN DISTINCT labels(a)[0] AS source, type(r) AS rel, labels(b)[0] AS target
        """)
        for record in rel_result:
            schema["relationships"].append({
                "source": record["source"],
                "relationship": record["rel"],
                "target": record["target"]
            })
    return schema

def format_schema_for_llm(schema):
    """Formats schema into text for the prompt."""
    schema_text = "Graph Database Schema:\n\nNodes and Properties:\n"
    for label, props in schema["nodes"].items():
        schema_text += f"- ({label}): properties {props}\n"
    schema_text += "\nRelationships:\n"
    for rel in schema["relationships"]:
        schema_text += f"- ({rel['source']})-[:{rel['relationship']}]->({rel['target']})\n"
    return schema_text


def generate_cypher(schema_text, user_question, previous_error=None, previous_query=None):
    """Generates or corrects a Cypher query using Qwen on Groq, stripping think tags."""
    
    error_context = ""
    if previous_error:
        error_context = f"\n\nPREVIOUS ATTEMPT FAILED:\nQuery: {previous_query}\nError: {previous_error}\nFix the query based on this error. Do NOT output any thinking process or markdown."

    system_prompt = f"""
You are an expert Neo4j Cypher query generator. 
Convert natural language questions into valid Cypher queries based ONLY on the provided graph schema.
Output ONLY the raw Cypher query code. Do not include markdown blocks, explanations, or conversational text.

{schema_text}
{error_context}
"""
    
    response = client.chat.completions.create(
        model="qwen/qwen3.6-27b",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question}
        ],
        temperature=0.0
    )
    
    raw_content = response.choices[0].message.content.strip()
    
    # Strip out <think>...</think> blocks if the model includes them
    cleaned_content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL)
    
    # Clean up markdown code blocks if present
    cleaned_content = cleaned_content.replace("```cypher", "").replace("```", "").strip()
    
    return cleaned_content

def execute_cypher_with_self_correction(driver, schema_text, user_question, max_retries=3):
    """Executes the query with a self-correction loop if errors occur."""
    current_query = generate_cypher(schema_text, user_question)
    
    for attempt in range(max_retries):
        print(f"\n[Attempt {attempt + 1}] Executing Query:\n{current_query}")
        try:
            with driver.session() as session:
                result = session.run(current_query)
                records = [record.data() for record in result]
                return current_query, records
        except Exception as e:
            print(f"❌ Execution Error: {e}")
            if attempt < max_retries - 1:
                print("🔄 Sending error back to LLM for self-correction...")
                current_query = generate_cypher(schema_text, user_question, previous_error=str(e), previous_query=current_query)
            else:
                raise e

if __name__ == "__main__":
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    try:
        # 1. Setup schema
        raw_schema = get_graph_schema(driver)
        schema_text = format_schema_for_llm(raw_schema)
        
        # 2. Test full end-to-end question
        user_question = "Find all people who own a company."
        print(f"User Question: '{user_question}'")
        
        # 3. Generate, Execute & Self-Correct
        final_query, query_results = execute_cypher_with_self_correction(driver, schema_text, user_question)
        
        print("\n--- RESULTS ---")
        print(f"Successful Cypher: {final_query}")
        print(f"Data returned: {query_results}")
        
    finally:
        driver.close()