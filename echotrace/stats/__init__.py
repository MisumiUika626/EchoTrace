import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime

from echotrace.ingest.parsers import TZ
from echotrace.util import AppError

POS = ("开心", "喜欢", "谢谢", "好呀", "期待", "哈哈", "关心", "想你", "加油", "辛苦")
NEG = ("生气", "难过", "烦", "讨厌", "不想", "失望", "别理", "累", "焦虑", "抱歉")
FUTURE = ("下次", "以后", "周末", "回头", "到时候", "一起", "约", "见面", "计划")
PRIVATE = ("家里", "父母", "身体", "生病", "工作", "烦恼", "压力", "失眠")
TRUST = ("帮我", "你觉得", "秘密", "告诉你", "怎么办", "建议", "拜托")
LABELS = {"affinity": "好感度", "intimacy": "亲密度", "trust": "信任度", "dependence": "依赖度",
          "initiative": "主动度", "stability": "情绪稳定度", "openness": "话题开放度", "progression": "关系推进意愿"}


def quantile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[int((len(ordered) - 1) * q)], 1)


def calculate(db, contact_id, start=None, end=None):
    session = db.one("SELECT * FROM sessions WHERE contact_id=?", (contact_id,))
    if not session:
        raise AppError("NOT_FOUND", "未找到此人物的会话。", 404)
    conditions, args = ["session_id=?"], [session["id"]]
    if start is not None:
        conditions.append("ts>=?")
        args.append(start)
    if end is not None:
        conditions.append("ts<=?")
        args.append(end)
    counts, chars, kinds, hours, months, tokens = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    signals, evidence, initiations = Counter(), defaultdict(list), Counter()
    latencies, turns, polarity = [], Counter(), Counter()
    previous, first, last, turn_count, night, total, media, latency_count = None, None, None, 0, 0, 0, 0, 0
    aliases, examples = defaultdict(list), defaultdict(list)
    with db.connect() as conn:
        for row in conn.execute("SELECT id,sender,sender_raw,ts,kind,text FROM messages WHERE " + " AND ".join(conditions) + " ORDER BY ts,id", args):
            mid, speaker, name, ts, kind, text = row
            dt = datetime.fromtimestamp(ts, TZ)
            first = ts if first is None else first
            last = ts
            total += 1
            counts[speaker] += 1
            chars[speaker] += len(text or "")
            kinds[kind] += 1
            hours[str(dt.hour)] += 1
            months[dt.strftime("%Y-%m")] += 1
            night += dt.hour >= 23 or dt.hour < 3
            media += kind in ("image", "emoji", "voice", "video", "file")
            if len(examples[speaker]) < 15:
                examples[speaker].append(mid)
            if name in aliases:
                aliases[name][1] = ts
            elif len(aliases) < 1000:
                aliases[name] = [ts, ts, mid]
            if not previous or ts - previous["ts"] > 21600:
                initiations[speaker] += 1
            if previous and speaker != previous["sender"] and speaker in ("me", "them"):
                delay = ts - previous["ts"]
                if previous["sender"] == "me" and speaker == "them":
                    latency_count += 1
                    if len(latencies) < 100000:
                        latencies.append(delay)
                if delay < 300:
                    turn_count += 1
                else:
                    turns[str(turn_count)] += 1
                    turn_count = 0
            if speaker == "them":
                terms = re.findall(r"[\u4e00-\u9fff]{2,4}|[a-zA-Z]{3,}", text or "")
                # bounded vocabulary; long-tail tokens do not grow memory with file size
                for term in terms:
                    if term in tokens or len(tokens) < 10000:
                        tokens[term] += 1
                positive, negative = any(w in text for w in POS), any(w in text for w in NEG)
                polarity["positive" if positive else "negative" if negative else "neutral"] += 1
                matches = {"affinity": positive, "intimacy": any(w in text for w in PRIVATE) or dt.hour >= 23 or dt.hour < 3,
                           "trust": any(w in text for w in TRUST), "dependence": any(w in text for w in ("帮我", "怎么办", "在吗")),
                           "initiative": not previous or ts - previous["ts"] > 21600,
                           "stability": positive or negative, "openness": bool(terms), "progression": any(w in text for w in FUTURE)}
                for key, match in matches.items():
                    signals[key] += bool(match)
                    if match and len(evidence[key]) < 12:
                        evidence[key].append(mid)
            previous = {"ts": ts, "sender": speaker}
    if turn_count:
        turns[str(turn_count)] += 1
    n = max(1, counts["them"])
    denominator = sum(tokens.values()) or 1
    entropy = -sum((v / denominator) * math.log2(v / denominator) for v in tokens.values())
    negative_rate = polarity["negative"] / n
    positive_rate = polarity["positive"] / n
    from echotrace.config import Config
    scales = Config(db).get()["radar"]["scales"]
    anchors = {
        "affinity": min(100, max(0, 50 + 100 * scales["affinity"] / 3 * (positive_rate - negative_rate))),
        "intimacy": min(100, signals["intimacy"] / n * 100 * scales["intimacy"]),
        "trust": min(100, signals["trust"] / n * 100 * scales["trust"]),
        "dependence": min(100, signals["dependence"] / n * 100 * scales["dependence"]),
        "initiative": min(100, 100 * scales["initiative"] * initiations["them"] / max(1, sum(initiations.values()))),
        "stability": max(0, 100 * (1 - min(1, negative_rate * 3 * scales["stability"]))),
        "openness": min(100, entropy / 8 * 100 * scales["openness"]),
        "progression": min(100, signals["progression"] / n * 100 * scales["progression"]),
    }
    missing = []
    if first and last:
        current = datetime.fromtimestamp(first, TZ).replace(day=1)
        end_dt = datetime.fromtimestamp(last, TZ)
        while (current.year, current.month) <= (end_dt.year, end_dt.month):
            key = current.strftime("%Y-%m")
            if key not in months:
                missing.append(key)
            current = current.replace(year=current.year + (current.month == 12), month=current.month % 12 + 1)
    return {"total": total, "first_ts": first, "last_ts": last, "counts": dict(counts), "kinds": dict(kinds),
            "media_count": media, "hours": dict(hours), "months": dict(months), "missing_months": missing,
            "message_ratio": counts["them"] / max(1, counts["me"]), "initiation_rate": anchors["initiative"] / 100,
            "reply_p50": quantile(latencies, .5), "reply_p90": quantile(latencies, .9), "latency_sampled": latency_count > len(latencies),
            "average_chars": {k: round(chars[k] / max(1, counts[k]), 1) for k in counts},
            "night_ratio": night / max(1, total), "turns": dict(turns), "emoji_per_100": kinds["emoji"] / max(1, total) * 100,
            "topic_entropy": round(entropy, 3), "topics": tokens.most_common(20), "future_per_1000": signals["progression"] / n * 1000,
            "emotions": {k: round(polarity[k] / n, 4) for k in ("positive", "negative", "neutral")},
            "emotion_variance": positive_rate + negative_rate - (positive_rate - negative_rate) ** 2,
            "aliases": [{"name": k, "first_ts": v[0], "last_ts": v[1], "evidence": [v[2]]} for k, v in aliases.items()],
            "anchors": {k: {"score": round(v), "evidence": evidence[k], "hits": signals[k], "sample": counts["them"],
                            "scale": scales[k],
                            "mapping_hint": "本地词频/时间/轮次启发式锚点，非心理测评；稀疏信号不代表真实人格。"} for k, v in anchors.items()},
            "examples": dict(examples), "notes": ["缺失月份仅代表文件内无记录，不能证明服务端记录完整。", "情感与话题使用本地词典近似计算。"]}
