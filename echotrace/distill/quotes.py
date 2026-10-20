def validate_quotes(output, messages):
    lookup = {m["id"]: m for m in messages}
    for quote in output["quotes"]:
        originals = [lookup[mid]["text"] for mid in quote["evidence"] if mid in lookup and lookup[mid]["sender"] == "them"]
        if not originals or not any(quote["text"] in text for text in originals):
            raise ValueError("语料必须逐字出自目标人物的原话")
