def pending_topics(items):
    last = {m['topic_id']: m for m in items}
    return sorted(topic for topic, m in last.items() if m['kind'] == 'question')
