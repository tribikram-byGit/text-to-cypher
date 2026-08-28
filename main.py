import os
import re
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from neo4j import GraphDatabase
from groq import Groq
from fastapi.middleware.cors import CORSMiddleware

# --- CONFIGURATION ---
NEO4J_URI = "neo4j+ssc://360d05b3.databases.neo4j.io"
NEO4J_AUTH = ("360d05b3", "pRBvI7D4Sd5fMVEgrCG2kh6UJrOQQhqY3kfuSTNWPek")
GROQ_API_KEY = "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"

app = FastAPI(
    title="AML Fraud Graph Copilot API",
    version="1.0.0",
    description="Production-grade Text2Cypher backend service with safety guardrails and multi-hop traversal."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all HTTP methods (POST, GET, etc.)
    allow_headers=["*"],  # Allows all headers
)

client = Groq(api_key=GROQ_API_KEY)

# --- PYDANTIC SCHEMAS (Request/Response validation) ---
class QueryRequest(BaseModel):
    question: str
    chat_history: Optional[List[Dict[str, str]]] = []  # [{"question": "...", "query": "..."}]

class GraphNode(BaseModel.ConfigDict if hasattr(BaseModel, "ConfigDict") else object):
    pass

class QueryResponse(BaseModel):
    user_question: str
    generated_cypher: str
    results: List[Dict[str, Any]]
    graph_data: Dict[str, List[Any]]

# --- NEO4J & GRAPH UTILITIES ---
def get_neo4j_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)

@app.on_event("startup")
def startup_event():
    """Initializes and seeds the enterprise AML schema on app startup."""
    driver = get_neo4j_driver()
    try:
        with driver.session() as session:
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
        print("✅ Enterprise AML Database Schema seeded successfully on startup!")
    finally:
        driver.close()

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

def safety_guardrail_check(cypher_query: str):
    """Blocks write/destructive operations to safeguard database integrity."""
    forbidden_keywords = ["DELETE", "DROP", "CREATE", "SET", "REMOVE", "MERGE", "DETACH"]
    upper_query = cypher_query.upper()
    
    for keyword in forbidden_keywords:
        if f" {keyword} " in upper_query or upper_query.startswith(keyword):
            raise HTTPException(status_code=403, detail=f"Security Guardrail Block: Unauthorized write operation '{keyword}' detected.")
    
    if "RETURN" not in upper_query:
        raise HTTPException(status_code=400, detail="Security Guardrail Block: Query must include a RETURN statement.")
    return True

def clean_llm_output(raw_content: str) -> str:
    cleaned = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<.*?>', '', cleaned)
    cleaned = cleaned.replace("```cypher", "").replace("```", "").strip()
    return cleaned

def generate_cypher_with_memory(schema_text: str, chat_history: List[Dict[str, str]], user_question: str, previous_error: Optional[str] = None, previous_query: Optional[str] = None) -> str:
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

# --- API ENDPOINT ---
@app.post("/api/v1/query", response_model=QueryResponse)
async def process_natural_language_query(payload: QueryRequest):
    driver = get_neo4j_driver()
    try:
        # 1. Fetch live schema context
        raw_schema = get_graph_schema(driver)
        schema_text = format_schema_for_llm(raw_schema)
        
        current_query = generate_cypher_with_memory(schema_text, payload.chat_history, payload.question)
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # 2. Enforce Security Guardrail
                safety_guardrail_check(current_query)
                
                # 3. Execute against Neo4j
                with driver.session() as session:
                    result = session.run(current_query)
                    records = [record.data() for record in result]

                    nodes_dict = {}
                    links = []
                    
                    for record in records:
                        for key, val in record.items():
                            if isinstance(val, dict) and ("id" in val or "name" in val or "account_number" in val):
                                node_id = str(val.get("id") or val.get("name") or val.get("account_number"))
                                nodes_dict[node_id] = {
                                    "id": node_id,
                                    "label": key,
                                    "properties": val
                                }
                    
                    graph_payload = {
                        "nodes": list(nodes_dict.values()),
                        "links": links
                    }

                    return QueryResponse(
                        user_question=payload.question,
                        generated_cypher=current_query,
                        results=records,
                        graph_data=graph_payload
                    )
            except HTTPException as he:
                raise he
            except Exception as e:
                if attempt < max_retries - 1:
                    # Self-correction retry loop
                    current_query = generate_cypher_with_memory(
                        schema_text, payload.chat_history, payload.question, 
                        previous_error=str(e), previous_query=current_query
                    )
                else:
                    raise HTTPException(status_code=500, detail=f"Database Execution Error after retries: {str(e)}")
    finally:
        driver.close()