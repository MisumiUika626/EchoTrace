"""Generate entirely fictional conversations. No personal data or network requests."""
import argparse
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

PAIRS = [
    ("今天写代码卡住了", "慢慢来呀，先吃点东西再试试"),
    ("周末要去图书馆吗", "好呀，下次一起去，我想看那本摄影书"),
    ("你最近在听什么", "循环一首虚构的歌《窗边的橘子》，听着很开心"),
    ("今天工作怎么样", "有一点累，谢谢你听我说，我先休息一下"),
    ("刚才是我着急了，抱歉", "我刚刚也有点生气。以后不开心我们直接说吧"),
    ("你觉得这个颜色怎么样", "我喜欢蓝色，像傍晚的天空"),
    ("要不要聊聊家里的事", "今天不太想聊这个，等我想说的时候再告诉你"),
    ("我买了两张虚构展览的票", "期待！周末一起去看展，下午两点见"),
    ("有什么需要我帮忙的吗", "你能帮我看看这个计划吗，你觉得先做哪一步"),
    ("最近好像都很忙", "是呀，但我还记得上次一起拍的照片，哈哈"),
    ("晚安啦", "晚安，明天记得吃早餐"),
    ("刚才没及时回你", "没关系，你忙完再回就好，我也去做自己的事"),
]


def generate(path: Path, count=5000):
    path.parent.mkdir(parents=True, exist_ok=True)
    base = int(datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=8))).timestamp())
    with path.open("w", encoding="utf-8") as handle:
        for i in range(count):
            day, position = divmod(i, 40)
            me = i % 2 == 0
            content = PAIRS[(i // 2) % len(PAIRS)][0 if me else 1]
            kind = "image" if i % 53 == 0 else "emoji" if i % 31 == 0 else "text"
            row = {"msgId": f"fiction-{i:08d}", "sender": {"uid": "fiction-me" if me else "fiction-them", "name": "小舟（虚构）" if me else "林间（虚构）"},
                   "msgTime": base + day * 86400 + position * 60 + (50400 if day % 7 == 0 else 0),
                   "kind": kind, "text": "[图片]" if kind == "image" else "[表情:微笑]" if kind == "emoji" else content,
                   "fixture": True}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=5000)
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/fictional.jsonl"))
    args = parser.parse_args()
    print(generate(args.output, args.count))
