'use client';

import React, { useState, useEffect, useRef } from 'react';
import dynamic from 'next/dynamic';

// Dynamically import force-graph to prevent Next.js SSR window-reference errors
const ForceGraph2D = dynamic(() => import('react-force-graph-2d'), { ssr: false });

type Message = {
  sender: 'user' | 'copilot';
  question?: string;
  cypher?: string;
  results?: any[];
  graphData?: { nodes: any[]; links: any[] };
  error?: string;
};

export default function FraudCopilotDashboard() {
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [chatHistory, setChatHistory] = useState<{ question: string; query: string }[]>([]);
  
  // Track active visual tab per message ('graph' or 'json')
  const [activeTab, setActiveTab] = useState<{ [key: number]: 'graph' | 'json' }>({});

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!input.trim() || loading) return;

    const userQuery = input;
    setInput('');
    setLoading(true);

    setMessages((prev) => [...prev, { sender: 'user', question: userQuery }]);

    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/query`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: userQuery, chat_history: chatHistory }),
      });

      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Something went wrong');

      const messageIndex = messages.length + 1;
      setMessages((prev) => [
        ...prev,
        {
          sender: 'copilot',
          cypher: data.generated_cypher,
          results: data.results,
          graphData: data.graph_data,
        },
      ]);

      setActiveTab((prev) => ({ ...prev, [messageIndex]: 'graph' }));
      setChatHistory((prev) => [...prev, { question: userQuery, query: data.generated_cypher }]);
    } catch (err: any) {
      setMessages((prev) => [...prev, { sender: 'copilot', error: err.message }]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="flex min-h-screen flex-col bg-slate-950 text-slate-100 p-6">
      <header className="mb-6 border-b border-slate-800 pb-4 flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold flex items-center gap-2">
            AML Fraud Graph Copilot
          </h1>
          <p className="text-sm text-slate-400">Enterprise Visual Compliance & Investigation Workspace</p>
        </div>
      </header>

      {/* Chat Feed */}
      <div className="flex-1 overflow-y-auto space-y-6 mb-6 max-w-5xl w-full mx-auto">
        {messages.length === 0 && (
          <div className="text-center text-slate-500 mt-20">
            <p className="text-lg">Ask a question to query and visually map your Neo4j compliance graph.</p>
            <p className="text-sm mt-2">Example: "Find all people who own a company."</p>
          </div>
        )}

        {messages.map((msg, idx) => (
          <div key={idx} className={`flex flex-col ${msg.sender === 'user' ? 'items-end' : 'items-start'}`}>
            {msg.sender === 'user' ? (
              <div className="bg-blue-600 text-white px-4 py-2 rounded-lg max-w-lg text-sm">
                {msg.question}
              </div>
            ) : (
              <div className="bg-slate-900 border border-slate-800 rounded-lg p-4 max-w-3xl w-full space-y-4 shadow-xl">
                {msg.error ? (
                  <p className="text-red-400 text-sm">❌ {msg.error}</p>
                ) : (
                  <>
                    {/* Generated Cypher Block */}
                    <div>
                      <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider">Generated Cypher</span>
                      <pre className="bg-slate-950 p-2 rounded text-emerald-400 text-xs overflow-x-auto mt-1 border border-slate-800">
                        {msg.cypher}
                      </pre>
                    </div>

                    {/* View Toggle Tabs */}
                    <div className="flex border-b border-slate-800 gap-4 text-xs font-medium">
                      <button
                        onClick={() => setActiveTab({ ...activeTab, [idx]: 'graph' })}
                        className={`pb-2 border-b-2 transition ${activeTab[idx] !== 'json' ? 'border-blue-500 text-blue-400' : 'border-transparent text-slate-400'}`}
                      >
                        Visual Network Graph
                      </button>
                      <button
                        onClick={() => setActiveTab({ ...activeTab, [idx]: 'json' })}
                        className={`pb-2 border-b-2 transition ${activeTab[idx] === 'json' ? 'border-blue-500 text-blue-400' : 'border-transparent text-slate-400'}`}
                      >
                        JSON Data Table ({msg.results?.length || 0})
                      </button>
                    </div>

                    {/* Tab Content Display */}
                    {activeTab[idx] === 'json' ? (
                      <pre className="bg-slate-950 p-3 rounded text-slate-300 text-xs overflow-auto max-h-72 border border-slate-800">
                        {JSON.stringify(msg.results, null, 2)}
                      </pre>
                    ) : (
                      <div className="h-72 w-full bg-slate-950 rounded border border-slate-800 overflow-hidden relative">
                        {msg.graphData && msg.graphData.nodes.length > 0 ? (
                          <ForceGraph2D
                            width={700}
                            height={288}
                            graphData={msg.graphData}
                            nodeColor={(node: any) => (node.properties?.risk_score > 80 ? '#ef4444' : '#3b82f6')}
                            nodeVal={10}
                            linkColor={() => '#475569'}
                            // This draws the node's name/details directly onto the canvas next to the ball:
                            nodeCanvasObject={(node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
                              // 1. Draw the default circular node ball
                              const radius = 6;
                              ctx.beginPath();
                              ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI, false);
                              ctx.fillStyle = node.properties?.risk_score > 80 ? '#ef4444' : '#3b82f6';
                              ctx.fill();

                              // 2. Draw the text label right next to the node
                              const label = node.properties?.name || node.id;
                              const fontSize = 12 / globalScale;
                              ctx.font = `${fontSize}px Sans-Serif`;
                              ctx.fillStyle = '#f8fafc'; // Light text color (slate-50)
                              ctx.textAlign = 'left';
                              ctx.textBaseline = 'middle';
                              ctx.fillText(label, node.x + 10, node.y); // Offset text slightly to the right of the node
                            }}
                          />
                        ) : (
                          <div className="flex items-center justify-center h-full text-xs text-slate-500">
                            No visual nodes returned for this query.
                          </div>
                        )}
                      </div>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        ))}
        {loading && <div className="text-slate-400 text-sm animate-pulse max-w-5xl mx-auto">Traversing graph paths and rendering canvas...</div>}
      </div>

      {/* Input Form */}
      <form onSubmit={handleSubmit} className="max-w-5xl w-full mx-auto flex gap-2">
        <input
          type="text"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask about high-risk entities, accounts, or multi-hop paths..."
          className="flex-1 bg-slate-900 border border-slate-800 rounded-lg px-4 py-3 text-sm focus:outline-none focus:border-blue-500"
        />
        <button
          type="submit"
          disabled={loading}
          className="bg-blue-600 hover:bg-blue-500 px-6 py-3 rounded-lg text-sm font-medium transition disabled:opacity-50"
        >
          Query
        </button>
      </form>
    </main>
  );
}