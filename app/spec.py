"""Declarative setting definitions shared by channels, LLM providers (and later embeddings and stores).
The admin page draws a form from a list of these."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    """One setting of an adapter. The admin UI renders its form from these."""
    name: str
    label: str
    kind: str = "text"            # text | list | bool | choice | number | kbs (knowledge base picker)
    secret: bool = False          # stored encrypted, never shown again
    required: bool = False
    help: str = ""
    choices: tuple = ()           # for kind == "choice"
    default: object = None
