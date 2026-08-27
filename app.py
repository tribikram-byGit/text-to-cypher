import os
import re
from neo4j import GraphDatabase
from groq import Groq

# --- CONFIGURATION ---
NEO4J_URI = "neo4j+ssc://360d05b3.databases.neo4j.io"
NEO4J_AUTH = ("360d05b3", "pRBvI7D4Sd5fMVEgrCG2kh6UJrOQQhqY3kfuSTNWPek")
GROQ_API_KEY = "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"


client = Groq(api_key=GROQ_API_KEY)

def get_graph_schema(driver):
    """Introspects Neo4j safely, filtering out unlabeled nodes."""
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
            WHERE size(labels(a)) > 0 AND size(labels(b)) > 0
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
    schema_text = "Graph Database Schema:\n\nNodes and Properties:\n"
    for label, props in schema["nodes"].items():
        schema_text += f"- ({label}): properties {props}\n"
    schema_text += "\nValid Relationships:\n"
    for rel in schema["relationships"]:
        schema_text += f"- ({rel['source']})-[:{rel['relationship']}]->({rel['target']})\n"
    return schema_text

def clean_llm_output(raw_content):
    """
    Aggressively strips thinking blocks, stray tags, and markdown 
    so the clean query reaches Neo4j on Attempt 1.
    """
    # Remove everything inside <think> tags (including tags themselves)
    cleaned = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL | re.IGNORECASE)
    # Catch any unclosed or stray think tags if they happen to leak
    cleaned = re.sub(r'<.*?>', '', cleaned)
    # Remove markdown code formatting blocks
    cleaned = cleaned.replace("```cypher", "").replace("```", "").strip()
    return cleaned

def generate_cypher_with_memory(schema_text, chat_history, user_question, previous_error=None, previous_query=None):
    error_context = ""
    if previous_error:
        error_context = f"\n\nPREVIOUS ATTEMPT FAILED:\nQuery: {previous_query}\nError: {previous_error}\nFix the query based on this error."

    system_prompt = f"""
You are an expert Neo4j Cypher query generator for a financial fraud investigation system.
Convert natural language questions into valid Cypher queries based ONLY on the graph schema and history.

RULES:
1. Use ONLY the node labels and relationship types explicitly defined in the schema. Do not invent links.
2. For multi-hop queries, bridge entities using valid paths (e.g., variable-length paths like -[:TRANSACTED_WITH*0..3]->).
3. Output ONLY the raw Cypher query code. NO markdown blocks, NO explanations, NO conversational text, NO <think> tags.

EXAMPLE OF EXPECTED OUTPUT FORMAT:
MATCH (p:Person)-[:OWNS]->(c:Company) RETURN p

{schema_text}
{error_context}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for turn in chat_history:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["query"]})
    
    messages.append({"role": "user", "content": user_question})

    response = client.chat.completions.create(
        model="qwen/qwen3.6-27b",
        messages=messages,
        temperature=0.0
    )
    return clean_llm_output(response.choices[0].message.content.strip())

def execute_with_memory_loop(driver, schema_text, chat_history, user_question, max_retries=3):
    current_query = generate_cypher_with_memory(schema_text, chat_history, user_question)
    
    for attempt in range(max_retries):
        print(f"\n[Attempt {attempt + 1}] Executing Cleaned Query:\n{current_query}")
        try:
            with driver.session() as session:
                result = session.run(current_query)
                records = [record.data() for record in result]
                return current_query, records
        except Exception as e:
            print(f"❌ Execution Error: {e}")
            if attempt < max_retries - 1:
                current_query = generate_cypher_with_memory(schema_text, chat_history, user_question, previous_error=str(e), previous_query=current_query)
            else:
                raise e

if __name__ == "__main__":
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    
    try:
        raw_schema = get_graph_schema(driver)
        schema_text = format_schema_for_llm(raw_schema)
        
        chat_history = []
        
        q1 = "Find all people who own a company."
        print(f"User: {q1}")
        query1, res1 = execute_with_memory_loop(driver, schema_text, chat_history, q1)
        chat_history.append({"question": q1, "query": query1})
        print(f"Results: {res1}\n" + "-"*40)
        
        q2 = "Now, find any person with a risk score greater than 80 connected through companies to a bank account."
        print(f"User: {q2}")
        query2, res2 = execute_with_memory_loop(driver, schema_text, chat_history, q2)
        chat_history.append({"question": q2, "query": query2})
        print(f"Results: {res2}\n" + "-"*40)

    finally:
        driver.close()