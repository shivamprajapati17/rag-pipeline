import os
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Blockchain Career Assistant API")
FRONTEND_FILE = Path(__file__).with_name("index.html")

# Track initialization status
pinecone_connected = False
index = None
embeddings = None
llm = None
rag_available = False
llm_error = None

# Initialize Pinecone using raw client
try:
    from pinecone import Pinecone as PC
    pc = PC(api_key=os.getenv("PINECONE_API_KEY"))
    index_name = os.getenv("PINECONE_INDEX_NAME")
    index_host = os.getenv("PINECONE_INDEX_HOST")
    if index_host:
        index = pc.Index(host=index_host)
    elif index_name:
        index = pc.Index(index_name)
    else:
        raise ValueError("Set PINECONE_INDEX_HOST or PINECONE_INDEX_NAME")
    pinecone_connected = True
    print(f"Pinecone connected to index: {index_name or index_host}")
except Exception as e:
    print(f"Pinecone connection failed: {e}")

# Initialize NVIDIA NeMo components
try:
    from langchain_nvidia_ai_endpoints import ChatNVIDIA, NVIDIAEmbeddings
    NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")
    if not NVIDIA_API_KEY:
        raise ValueError("NVIDIA_API_KEY not set")

    embeddings = NVIDIAEmbeddings(
        model="nvidia/nemotron-3.5-embed-1b",
        dimensions=1024,
        nvidia_api_key=NVIDIA_API_KEY,
    )

    llm = ChatNVIDIA(
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        temperature=0.2,
        max_completion_tokens=2048,
        nvidia_api_key=NVIDIA_API_KEY,
    )
    llm_error = None
    print("NVIDIA NeMo initialized")

except ImportError:
    print("langchain-nvidia-ai-endpoints not installed, NVIDIA initialization skipped")
    llm_error = "langchain-nvidia-ai-endpoints module not installed"
    llm = None
    embeddings = None
except Exception as e:
    print(f"NVIDIA NeMo initialization error: {e}")
    llm_error = str(e)
    llm = None
    embeddings = None

# Build RAG chain if all components are available
if pinecone_connected and embeddings is not None and llm is not None:
    try:
        from langchain_core.prompts import ChatPromptTemplate

        system_prompt = (
            "You are an expert enterprise assistant. Use the following retrieved context to answer the user's question. "
            "If the context does not answer the question, say that you don't know. Do not add unsupported facts. "
            "Answer clearly and concisely."
            "\n\nContext: {context}"
        )

        # Retrieval function - query Pinecone vector store
        def retrieve(query_text, k=5):
            """Search Pinecone's integrated document embeddings."""
            try:
                from pinecone import EmbedQuery

                query_results = index.documents.search(
                    namespace="default",
                    score_by=[EmbedQuery(query=query_text, field="chunk_text")],
                    top_k=k,
                    include_fields=["chunk_text", "source", "page"],
                )
                return query_results.matches or []
            except Exception as e:
                print(f"Retrieval error: {e}")
                raise HTTPException(status_code=502, detail="Document search failed. Check Pinecone configuration.") from e

        # Format retrieved documents into context string
        def format_docs(docs):
            """Format retrieved documents into context string"""
            if not docs:
                return ""
            contexts = []
            for doc in docs:
                content = (
                    doc.get("chunk_text", "")
                    if isinstance(doc, dict)
                    else getattr(doc, "chunk_text", "")
                )
                contexts.append(content)
            return "\n\n".join(contexts)

        # Process a query through the RAG pipeline
        def process_query(query_text):
            """Process a query through the RAG pipeline"""
            docs = retrieve(query_text)
            context = format_docs(docs)

            # Format prompt with context
            full_prompt = system_prompt.replace("{context}", context) + f"\n\nQuestion: {query_text}"

            try:
                response = llm.invoke(full_prompt)
                answer = response.content if hasattr(response, 'content') else str(response)
            except Exception as e:
                print(f"Answer generation error: {e}")
                raise HTTPException(status_code=502, detail="Answer generation failed. Check NVIDIA configuration.") from e

            # Extract source metadata
            sources = []
            for doc in docs:
                if isinstance(doc, dict):
                    source = doc.get("source", "Unknown")
                    page = doc.get("page")
                else:
                    source = getattr(doc, "source", "Unknown")
                    page = getattr(doc, "page", None)
                if page is not None:
                    source = f"{source} (page {int(page)})"
                sources.append(source)

            return {"answer": answer, "sources": list(dict.fromkeys(sources))}

        rag_available = True

    except Exception as e:
        print(f"RAG chain setup error: {e}")
        rag_available = False
else:
    if llm_error:
        print(f"RAG disabled due to LLM error: {llm_error}")

# 2. Define Data Models
class QueryRequest(BaseModel):
    query: str

class QueryResponse(BaseModel):
    answer: str
    sources: list

@app.get("/")
async def root():
    return FileResponse(FRONTEND_FILE)

@app.get("/health")
async def health():
    return {
        "pinecone_connected": pinecone_connected,
        "index_name": os.getenv("PINECONE_INDEX_NAME", "not_set"),
        "embeddings_available": embeddings is not None,
        "llm_available": llm is not None,
        "rag_available": rag_available,
        "llm_error": llm_error,
    }

@app.post("/api/chat", response_model=QueryResponse)
async def chat_endpoint(request: QueryRequest):
    if not pinecone_connected:
        raise HTTPException(
            status_code=503,
            detail="Pinecone not available - please configure valid PINECONE_API_KEY in .env file"
        )

    if not rag_available:
        raise HTTPException(
            status_code=503,
            detail=f"RAG system not fully available - {llm_error or 'Google Generative AI components need configuration'}"
        )

    try:
        # Process query through RAG pipeline
        result = process_query(request.query)

        if not result["answer"]:
            return {"answer": "No relevant documents found in the vector store. Please ingest documents first using the ingest.py script.", "sources": []}

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)