// Rating under an answer: a thumbs-up and a thumbs-down. After a thumbs-down it opens a reason list, a comment box and an optional link, which
// are sent with the rating. A click calls the "caa_rate" action (webchat/chat.py) with the signed payload; the server decides whether it counts
// (only the asker, not expired) and masks the comment before storing it. It appears only where rating is on, and replaces Chainlit's own thumbs.
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";

export default function FeedbackRating() {
  const [state, setState] = useState("idle"); // idle | form | done
  const [reason, setReason] = useState("");
  const [comment, setComment] = useState("");
  const [link, setLink] = useState("");
  const labels = props.labels || {};
  const reasons = props.reasons || [];
  if (!props.payload) return null;

  function send(rating, extra) {
    callAction({ name: "caa_rate", payload: { payload: props.payload, rating, ...(extra || {}) } });
  }
  function up() {
    send("up");
    setState("done");
  }
  function down() {
    send("down");
    setState(reasons.length ? "form" : "done");
  }
  function submit() {
    send("down", { reason, comment, link });
    setState("done");
  }

  if (state === "done") {
    return <div className="mt-2 px-1 text-xs text-muted-foreground">{labels.thanks || ""}</div>;
  }
  const field = "w-full rounded-xl border border-border bg-card px-3 py-2 text-sm text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div className="mt-2 flex w-full flex-col gap-2">
      {state === "idle" ? (
        <div className="flex items-center gap-1">
          {labels.prompt ? <span className="mr-1 px-1 text-xs text-muted-foreground">{labels.prompt}</span> : null}
          <button type="button" onClick={up} title={labels.up || ""} aria-label={labels.up || "Helpful"}
            className="rounded-md p-2 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <ThumbsUp className="h-4 w-4" />
          </button>
          <button type="button" onClick={down} title={labels.down || ""} aria-label={labels.down || "Not helpful"}
            className="rounded-md p-2 text-muted-foreground hover:bg-accent hover:text-accent-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <ThumbsDown className="h-4 w-4" />
          </button>
        </div>
      ) : (
        <div className="flex w-full flex-col gap-2 rounded-xl border border-border p-3">
          <div className="text-xs font-medium text-muted-foreground">{labels.reason || ""}</div>
          <div className="flex flex-wrap gap-2">
            {reasons.map((code) => (
              <button key={code} type="button" onClick={() => setReason(code)}
                className={"rounded-xl border px-3 py-2 text-left text-sm text-foreground transition-colors hover:bg-accent focus:outline-none focus-visible:ring-2 focus-visible:ring-ring " +
                  (reason === code ? "border-primary bg-accent" : "border-border bg-card")}>
                {(labels.reason_labels || {})[code] || code}
              </button>
            ))}
          </div>
          <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} placeholder={labels.comment || ""} className={field} />
          <input type="text" value={link} onChange={(e) => setLink(e.target.value)} placeholder={labels.link || ""} className={field} />
          <div className="flex justify-end">
            <button type="button" onClick={submit}
              className="rounded-xl bg-primary px-4 py-2 text-sm text-primary-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring">
              {labels.submit || "Send"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
