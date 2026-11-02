// Suggested follow-up questions under an answer: a short heading, then one full-width row per question with an arrow.
// A click sends the signed payload to the "caa_suggest" action (webchat/chat.py), which asks it in the same conversation.
// The server decides whether the click counts (only the asker, once, not expired); the rows are disabled after a click.
import { ArrowRight } from "lucide-react";
import { useState } from "react";

export default function Suggestions() {
  const [chosen, setChosen] = useState(null);
  const items = props.items || [];
  if (!items.length) return null;

  function ask(item, index) {
    if (chosen !== null) return;
    setChosen(index);
    callAction({ name: "caa_suggest", payload: { payload: item.payload, text: item.text } });
  }

  return (
    <div className="mt-2 flex w-full flex-col gap-2">
      {props.title ? <div className="px-1 text-xs font-medium text-muted-foreground">{props.title}</div> : null}
      {items.map((item, i) => (
        <button
          key={i}
          type="button"
          disabled={chosen !== null}
          onClick={() => ask(item, i)}
          className={
            "group flex w-full items-center justify-between gap-3 rounded-xl border bg-card px-4 py-3 text-left text-sm " +
            "text-foreground transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring " +
            "disabled:cursor-default disabled:hover:bg-card " + (chosen === i ? "border-primary" : "border-border") +
            (chosen !== null && chosen !== i ? " opacity-50" : "")
          }
        >
          <span className="leading-snug">{item.text}</span>
          <ArrowRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-hover:translate-x-0.5" />
        </button>
      ))}
    </div>
  );
}
