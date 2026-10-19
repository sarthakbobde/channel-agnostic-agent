"""Child process for the kill test: starts answering a question, prints READY once the first token is out, then waits to be killed."""
import json, os, sys, time
sys.path.insert(0, os.getcwd())
from langchain_core.messages import AIMessage
from app import chat_service, rag, identity
from app.db import get_db
from app.knowledge import kbs


class Slow:
    def invoke(self, messages, **kw):
        q = str(messages[-1].content).split("Latest message:", 1)[-1].strip()
        return AIMessage(content=json.dumps({"search": False, "query": q}))

    def stream(self, messages, **kw):
        yield AIMessage(content="The answer begins")
        print("READY", flush=True)
        time.sleep(60)
        yield AIMessage(content=" and ends.")


chat_service.get_chat_model = lambda: Slow()
rag.get_chat_model = lambda: Slow()
tokens, meta = chat_service.chat_stream("What happens to my leave if the process dies?", "slack-T-room-kill1")
for t in tokens:
    pass
