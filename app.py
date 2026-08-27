import os
import re
from neo4j import GraphDatabase
from groq import Groq

# --- CONFIGURATION ---
NEO4J_URI = "neo4j+ssc://360d05b3.databases.neo4j.io"
NEO4J_AUTH = ("360d05b3", "pRBvI7D4Sd5fMVEgrCG2kh6UJrOQQhqY3kfuSTNWPek")
GROQ_API_KEY = "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"

client = Groq(api_key=GROQ_API_KEY)

def setup_enterprise_schema(driver):
    """
    Sets up a realistic enterprise AML/Fraud database schema 
    with proper multi-hop paths including company-to-bank account links.
    """
    with driver.session() as session:
        # Clear old mock data and seed enterprise model
        session.run("MATCH (n) DETACH DELETE n")
        
        session.run("""
            CREATE (p1:Person {name: "Alice Smith", id: "P001", risk_score: 15, kyc_status: "Verified"})
            CREATE (p2:Person {name: "Bob Jones", id: "P002", risk_score: 85, kyc_status: "Flagged"})
            CREATE (c1:Company {name: "Shell Corp Alpha", registration_number: "REG123", jurisdiction: "Panama"})
            CREATE (c2:Company {name: "Legit Tech Ltd", registration_number: "REG999", jurisdiction: "USA"})
            CREATE (b1:BankAcc {account_number: "ACC-9988", balance: 50000.00, status: "Active"})
            CREATE (b2:BankAcc {account_number: "ACC-5544", balance: 120000.00, status: "Under Review"})
            CREATE (ip:IPAddress {address: "192.168.1.100"})

            CREATE (p1)-[:OWNS]->(c1)
            CREATE (p2)-[:OWNS]->(c1)
            CREATE (p2)-[:CONTROLS]->(b1)
            CREATE (c1)-[:HAS_ACCOUNT]->(b1)
            CREATE (c2)-[:HAS_ACCOUNT]->(b2)
            CREATE (c1)-[:TRANSACTED_WITH {amount: 250000.00, timestamp: "2026-01-15"}]->(c2)
            CREATE (p2)-[:USED_IP]->(ip)
            CREATE (p1)-[:USED_IP]->(ip)
        """)
    print("✅ Enterprise AML Database Schema seeded successfully!")

def get_graph_schema(driver):
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

def safety_guardrail_check(cypher_query):
    """
    PRODUCTION GUARDRAIL: Blocks destructive database actions (writes/deletes) 
    and verifies that the query is a safe read operation.
    """
    forbidden_keywords = ["DELETE", "DROP", "CREATE", "SET", "REMOVE", "MERGE", "DETACH"]
    upper_query = cypher_query.upper()
    
    for keyword in forbidden_keywords:
        if f" {keyword} " in upper_query or upper_query.startswith(keyword):
            raise ValueError(f"🚨 Security Guardrail Block: Query contains unauthorized write operation '{keyword}'. Read-only access permitted.")
    
    if "RETURN" not in upper_query:
        raise ValueError("🚨 Security Guardrail Block: Query must include a RETURN statement.")
        
    return True

def clean_llm_output(raw_content):
    cleaned = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<.*?>', '', cleaned)
    cleaned = cleaned.replace("```cypher", "").replace("```", "").strip()
    return cleaned

def generate_cypher_with_memory(schema_text, chat_history, user_question, previous_error=None, previous_query=None):
    error_context = ""
    if previous_error:
        error_context = f"\n\nPREVIOUS ATTEMPT FAILED:\nQuery: {previous_query}\nError: {previous_error}\nFix the query based on this error."

    system_prompt = f"""
You are an expert Neo4j Cypher query generator for an enterprise financial crime compliance system.
Convert natural language questions into valid, read-only Cypher queries based ONLY on the graph schema.

RULES:
1. Use ONLY the node labels and relationship types explicitly defined in the schema.
2. For multi-hop path analysis, map explicit traversal hops (e.g., matching through -[:HAS_ACCOUNT]-> or -[:TRANSACTED_WITH]->).
3. Output ONLY the raw Cypher query code. NO markdown blocks, NO explanations, NO <think> tags.

EXAMPLE:
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

def execute_with_guardrail_and_loop(driver, schema_text, chat_history, user_question, max_retries=3):
    current_query = generate_cypher_with_memory(schema_text, chat_history, user_question)
    
    for attempt in range(max_retries):
        print(f"\n[Attempt {attempt + 1}] Generated Query:\n{current_query}")
        try:
            # 🛡️ STEP 1: Run through Security Guardrail before touching the database
            safety_guardrail_check(current_query)
            
            # 🚀 STEP 2: Execute safely if checks pass
            with driver.session() as session:
                result = session.run(current_query)
                records = [record.data() for record in result]
                return current_query, records
                
        except Exception as e:
            print(f"❌ Execution/Guardrail Error: {e}")
            if attempt < max_retries - 1:
                print("🔄 Sending error back to LLM for self-correction...")
                current_query = generate_cypher_with_memory(schema_text, chat_history, user_question, previous_error=str(e), previous_query=current_query)
            else:
                raise e

if __name__ == "__main__":
    driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
    
    try:
        # Setup real enterprise model
        # setup_enterprise_schema(driver)
        
        raw_schema = get_graph_schema(driver)
        schema_text = format_schema_for_llm(raw_schema)
        
        chat_history = []
        
        # Test Turn 1: Standard query
        q1 = "Find all people who own a company."
        print(f"\nUser: {q1}")
        query1, res1 = execute_with_guardrail_and_loop(driver, schema_text, chat_history, q1)
        chat_history.append({"question": q1, "query": query1})
        print(f"Results: {res1}\n" + "-"*40)
        
        # Test Turn 2: Multi-hop path query through the newly bridged relationship
        q2 = "Find any person with a risk score greater than 80 connected through companies to a bank account."
        print(f"User: {q2}")
        query2, res2 = execute_with_guardrail_and_loop(driver, schema_text, chat_history, q2)
        chat_history.append({"question": q2, "query": query2})
        print(f"Results: {res2}\n" + "-"*40)

    finally:
        driver.close()
