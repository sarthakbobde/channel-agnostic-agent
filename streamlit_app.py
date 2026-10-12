import os
import uuid
import requests
import streamlit as st

# Configuration
BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="RAG Assistant", page_icon="💬")
st.title("RAG Assistant")

# Check backend connection
try:
    response = requests.get(f"{BACKEND_URL}/health", timeout=2)
    backend_ok = response.status_code == 200
except requests.RequestException:
    backend_ok = False

if not backend_ok:
    st.error(f"❌ Cannot connect to backend at {BACKEND_URL}")
    st.info("Make sure the backend is running: `docker-compose up -d`")
    st.stop()

if "conv_id" not in st.session_state:
    st.session_state.conv_id = str(uuid.uuid4())

def new_chat() -> None:
    st.session_state.conv_id = str(uuid.uuid4())

# ---------- sidebar: history ----------
with st.sidebar:
    st.caption(f"🔗 Backend: {BACKEND_URL}")
    st.button("➕ New chat", on_click=new_chat, use_container_width=True)
    st.divider()
    st.caption("History")
    
    try:
        response = requests.get(f"{BACKEND_URL}/conversations", timeout=5)
        if response.status_code == 200:
            conversations = response.json()
            if conversations:
                for conv in conversations:
                    conv_id = conv["id"]
                    title = conv.get("title", "New chat")[:40]
                    active = conv_id == st.session_state.conv_id
                    col_open, col_del = st.columns([5, 1])
                    with col_open:
                        if st.button(
                            f"📝 {title}", 
                            key=f"open_{conv_id}", 
                            use_container_width=True,
                            type="primary" if active else "secondary",
                        ):
                            st.session_state.conv_id = conv_id
                            st.rerun()
                    with col_del:
                        if st.button("🗑", key=f"del_{conv_id}"):
                            requests.delete(f"{BACKEND_URL}/conversations/{conv_id}")
                            if active:
                                new_chat()
                            st.rerun()
            else:
                st.caption("No conversations yet. Start a new chat!")
        else:
            st.caption(f"Backend error: {response.status_code}")
    except requests.exceptions.ConnectionError:
        st.caption("Cannot reach backend")
    except requests.exceptions.Timeout:
        st.caption("Backend timeout")
    except Exception as e:
        st.caption(f"Error: {str(e)}")

# ---------- helpers ----------
def render_meta(data: dict) -> None:
    sources = data.get("sources", [])
    if sources:
        with st.expander(f"📚 Sources ({len(sources)})"):
            for s in sources:
                page = f", p.{s['page']}" if s.get("page") else ""
                st.caption(f"📄 [{s['n']}] {s['source']}{page} (score {s['score']})")

# ---------- main chat ----------
conv_id = st.session_state.conv_id

try:
    response = requests.get(f"{BACKEND_URL}/conversations/{conv_id}", timeout=5)
    if response.status_code == 200:
        history = response.json()
        for m in history.get("messages", []):
            with st.chat_message(m["role"]):
                st.write(m["content"])
                if m["role"] == "assistant":
                    render_meta(m.get("meta", {}))
except Exception as e:
    st.warning(f"Could not load conversation: {e}")

if prompt := st.chat_input("Ask a question about the knowledge base..."):
    with st.chat_message("user"):
        st.write(prompt)

    ok = False
    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking..."):
                response = requests.post(
                    f"{BACKEND_URL}/chat",
                    json={"message": prompt, "conv_id": conv_id},
                    timeout=30
                )
            
            if response.status_code == 200:
                data = response.json()
                st.write(data["answer"])
                render_meta(data)
                ok = True
            else:
                st.error(f"Backend error: {response.status_code}")
        except requests.exceptions.Timeout:
            st.error("Request timed out")
        except requests.exceptions.ConnectionError:
            st.error(f"Cannot connect to {BACKEND_URL}")
        except Exception as e:
            st.error(f"Error: {e}")

    if ok:
        st.rerun()