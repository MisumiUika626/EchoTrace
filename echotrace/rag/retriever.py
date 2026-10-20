import json
import struct
from collections import defaultdict

from .indexer import embed, tokenize, window


def search(db, config, contact_id, query, limit=8, speaker=None, start=None, end=None, topic=None):
    rows = db.rows("SELECT * FROM quotes WHERE contact_id=?", (contact_id,))
    candidates = {q["id"]: q for q in rows if (not speaker or q["speaker"] == speaker)
                  and (start is None or q["ts"] >= start) and (end is None or q["ts"] <= end)
                  and (not topic or q["topic"] == topic)}
    scores = defaultdict(float)
    terms = tokenize(query)
    if terms:
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:30])
        with db.connect() as conn:
            results = conn.execute("SELECT rowid,bm25(quotes_fts) AS rank FROM quotes_fts WHERE quotes_fts MATCH ? AND contact_id=? ORDER BY rank LIMIT 50", (match, contact_id)).fetchall()
        for rank, row in enumerate(results):
            if row["rowid"] in candidates:
                scores[row["rowid"]] += 1 / (60 + rank + 1)
        vector = embed(query, config)
        # Bound to this person's corpus before ranking, preventing cross-person contamination.
        qvec = struct.unpack("256f", vector)
        ranked = []
        if db.vector_available and any(qvec):
            with db.connect() as conn:
                db.load_vec(conn)
                results = conn.execute("""SELECT q.id,vec_distance_cosine(v.embedding,?) AS distance
                    FROM quotes q JOIN quote_vectors v ON v.id=q.id WHERE q.contact_id=?
                    AND (? IS NULL OR q.speaker=?) AND (? IS NULL OR q.ts>=?)
                    AND (? IS NULL OR q.ts<=?) AND (? IS NULL OR q.topic=?) ORDER BY distance LIMIT 50""",
                    (vector, contact_id, speaker, speaker, start, start, end, end, topic, topic)).fetchall()
                ranked = [(1 - row["distance"], row["id"]) for row in results if row["distance"] is not None and row["distance"] < .95]
        elif not db.vector_available:
            for mid, row in candidates.items():
                if row["embedding"]:
                    similarity = sum(a * b for a, b in zip(qvec, struct.unpack("256f", row["embedding"])))
                    if similarity > .05:
                        ranked.append((similarity, mid))
        for rank, (_, mid) in enumerate(sorted(ranked, reverse=True)[:50]):
            scores[mid] += 1 / (60 + rank + 1)
    else:
        for rank, row in enumerate(sorted(candidates.values(), key=lambda x: (x["source"] == "user_pick", x["score"], x["ts"]), reverse=True)):
            scores[row["id"]] = 1 / (rank + 1)
    ordered = sorted(scores, key=lambda mid: scores[mid] * (1.15 if candidates[mid]["source"] == "user_pick" else 1), reverse=True)[:limit]
    result = []
    for mid in ordered:
        row = {k: v for k, v in candidates[mid].items() if k != "embedding"}
        row["evidence"] = json.loads(row["evidence"])
        row["dims"] = json.loads(row["dims"])
        row["context"] = window(db, contact_id, row["evidence"][0])
        row["relevance"] = round(scores[mid], 5)
        result.append(row)
    return result
