import os
import uuid
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

# Configuration
BACKEND_URL = os.getenv("RAG_BACKEND_URL", "http://localhost:8000")

st.set_page_config(page_title="Channel Agnostic Agent", page_icon="💬")
st.title("Channel Agnostic Agent")

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


# ---------- sign-in ----------
def sign_out(notice: str = "") -> None:
    for k in ("token", "user_name", "user_email", "conv_id"):
        st.session_state.pop(k, None)
    if notice:
        st.session_state.notice = notice


def set_password_page(token: str) -> None:
    """Opened from a one-time link an Admin gave this person."""
    st.subheader("Choose your password")
    with st.form("setpw"):
        pw = st.text_input("New password (12+ characters)", type="password")
        again = st.text_input("Repeat password", type="password")
        go = st.form_submit_button("Save password")
    if go:
        if pw != again:
            st.error("The two passwords do not match.")
        else:
            try:
                r = requests.post(f"{BACKEND_URL}/chat-auth/set-password", json={"token": token, "password": pw}, timeout=10)
                if r.status_code == 200:
                    st.query_params.clear()
                    st.session_state.notice = "Password saved. Sign in with your email and the new password."
                    st.rerun()
                else:
                    st.error(r.json().get("detail", "That did not work."))
            except requests.RequestException:
                st.error("Cannot reach the backend.")
    st.stop()


def sign_in_page() -> None:
    st.subheader("Sign in")
    if st.session_state.get("notice"):
        st.info(st.session_state.pop("notice"))
    with st.form("login"):
        email = st.text_input("Email")
        pw = st.text_input("Password", type="password")
        go = st.form_submit_button("Sign in")
    if go:
        try:
            r = requests.post(f"{BACKEND_URL}/chat-auth/login", json={"email": email, "password": pw}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                st.session_state.update(token=d["token"], user_name=d.get("name") or d["email"], user_email=d["email"])
                st.rerun()
            else:
                st.error(r.json().get("detail", "Sign-in failed."))
        except requests.RequestException:
            st.error("Cannot reach the backend.")
    st.stop()


if st.query_params.get("setpw"):
    set_password_page(st.query_params["setpw"])
if not st.session_state.get("token"):
    sign_in_page()
HEADERS = {"Authorization": f"Bearer {st.session_state.token}"}


def check_session(response) -> None:
    """A refused token means the session ended: go back to the sign-in page."""
    if response.status_code in (401, 503):
        sign_out("Your session ended. Please sign in again.")
        st.rerun()

if "conv_id" not in st.session_state:
    st.session_state.conv_id = str(uuid.uuid4())

def new_chat() -> None:
    st.session_state.conv_id = str(uuid.uuid4())

# ---------- sidebar: history ----------
with st.sidebar:
    st.caption(f"Signed in as {st.session_state.get('user_name', '')}")
    st.button("Sign out", on_click=sign_out, use_container_width=True)
    st.button("➕ New chat", on_click=new_chat, use_container_width=True)
    st.divider()
    st.caption("History")
    
    try:
        response = requests.get(f"{BACKEND_URL}/conversations", headers=HEADERS, timeout=5)
        check_session(response)
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
                            requests.delete(f"{BACKEND_URL}/conversations/{conv_id}", headers=HEADERS)
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
    response = requests.get(f"{BACKEND_URL}/conversations/{conv_id}", headers=HEADERS, timeout=5)
    check_session(response)
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
                    headers=HEADERS,
                    json={"message": prompt, "conv_id": conv_id},
                    timeout=30
                )
            
            check_session(response)
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