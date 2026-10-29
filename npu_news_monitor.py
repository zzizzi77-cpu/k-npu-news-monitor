#!/usr/bin/env python3
"""
K-NPU News Monitor
Daily Korean media monitoring for FuriosaAI, Rebellions, and DeepX.

Collects Google News RSS results, classifies each headline by topic, subject
(who the story is about), and tone, then writes a one-page Markdown brief.

Usage:
    python npu_news_monitor.py                 # last 1 day
    python npu_news_monitor.py --days 7        # last 7 days
    python npu_news_monitor.py --days 180 --out brief.md
    python npu_news_monitor.py --from-file raw_headlines.md   # offline, no network

Optional: set ANTHROPIC_API_KEY and pass --llm to add a 5-line executive
summary written by Claude on top of the rule-based classification.
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

COMPANIES = {
    "FuriosaAI": ["퓨리오사AI", "퓨리오사", "FuriosaAI", "레니게이드", "RNGD"],
    "Rebellions": ["리벨리온", "Rebellions", "아톰맥스", "ATOM-Max"],
    "DeepX": ["딥엑스", "DeepX"],
}

TOPIC_RULES = [
    ("이슈·부정", ["특혜", "특별대우", "미집행", "좌불안석", "뒷말", "우려", "시험대", "무게감", "매출의 질", "엑시트 딜레마", "논란"]),
    ("투자·IPO·정책자금", ["IPO", "상장", "프리IPO", "국민성장펀드", "투자", "밸류", "기업가치", "RCPS", "증권", "코스닥", "코스피"]),
    ("M&A·빅테크", ["인수", "M&A", "젠슨", "엔비디아와", "회동", "메타"]),
    ("해외 사업", ["유럽", "스웨덴", "스톡홀름", "리스본", "에퀴닉스", "싱가포르", "사우디", "아람코", "영국", "일본", "중동", "수출", "글로벌"]),
    ("고객 상용화 (수치)", ["토큰", "대 투입", "만대", "장 공급", "랙", "공공 CCTV", "변전소", "상용", "가동", "구동"]),
    ("파트너십·MOU·실증", ["MOU", "협약", "협력", "손잡", "맞손", "공동", "실증", "생태계", "뭉쳤", "탑재", "품었", "도입", "구축한다", "개발한다", "만든다"]),
    ("제품·기술·연구", ["3세대", "HBM", "증산", "논문", "ICML", "서버", "성능", "백서", "칩", "NPU"]),
    ("CEO·인물", ["대표", "백준호", "박성현", "인터뷰", "창업"]),
    ("정부·공공", ["정부", "과기정통부", "금융위", "국회", "혁신제품", "조달", "경북", "서울대"]),
]

NEG_WORDS = ["특혜", "특별대우", "미집행", "좌불안석", "뒷말", "우려", "시험대", "무게감", "낮아", "지연", "논란", "결별", "늦춘다"]
POS_WORDS = ["성공", "입증", "확정", "선정", "첫", "최초", "확대", "공급", "채택", "승인", "유치"]


def fetch_rss(query: str, days: int):
    q = urllib.parse.quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = it.findtext("pubDate") or ""
        src = it.findtext("source") or ""
        link = it.findtext("link") or ""
        items.append({"title": title, "date": pub[5:16], "source": src, "link": link})
    return items


def load_from_file(path: str):
    """Offline input: markdown with '## <Company>' sections and 'MM-DD 매체 | 제목' lines."""
    data, cur = {c: [] for c in COMPANIES}, None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if line.startswith("## "):
            name = line[3:].split(" ")[0]
            cur = next((c for c, ns in COMPANIES.items() if name in ns or name == c), None)
            continue
        m = re.match(r"^(\d\d-\d\d)\s+(\S+)\s+\|\s+(.*)$", line)
        if cur and m:
            data[cur].append({"title": m.group(3), "date": m.group(1), "source": m.group(2), "link": ""})
    return data


def classify_topic(title: str) -> str:
    for topic, kws in TOPIC_RULES:
        if any(k in title for k in kws):
            return topic
    return "기타"


def classify_subject(title: str, company: str) -> str:
    """Is the company the grammatical subject, or does a partner lead the headline?

    Rule: strip leading tags ([단독], [AI픽], quotes), then the first entity before the
    first comma, middle dot or space must be the company itself.
    """
    names = COMPANIES[company]
    if not any(n in title for n in names):
        return "언급"
    head = re.sub(r"^(\[[^\]]*\]\s*)+", "", title).strip()
    # a leading quoted phrase ("...") followed by dots is a hook, not the subject
    head = re.sub(r"^[\"“'‘][^\"”'’]*[\"”'’]\s*[.…]*\s*", "", head).strip()
    lead = re.split(r"[,·\s…]", head, maxsplit=1)[0]
    if any(n in lead for n in names):
        return "자사 주어"
    # "A·B·C 3사" style multi-company headlines: company mentioned but not leading
    return "파트너 주어"


def classify_tone(title: str) -> str:
    if any(w in title for w in NEG_WORDS):
        return "부정"
    if any(w in title for w in POS_WORDS):
        return "긍정"
    return "중립"


def dedupe(items):
    seen, out = set(), []
    for it in items:
        key = re.sub(r"\s+", "", it["title"])[:40]
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def build_brief(data: dict, days: int) -> str:
    today = dt.date.today().isoformat()
    lines = [f"# K-NPU 보도 브리프 ({today}, 최근 {days}일)", ""]
    lines.append("| 회사 | 건수 | 자사 주어 비율 | 부정 | 상위 주제 |")
    lines.append("|---|---|---|---|---|")
    for comp, items in data.items():
        n = len(items)
        if n == 0:
            lines.append(f"| {comp} | 0 | - | - | - |")
            continue
        own = sum(1 for i in items if i["subject"] == "자사 주어")
        neg = sum(1 for i in items if i["tone"] == "부정")
        top = Counter(i["topic"] for i in items).most_common(2)
        top_s = ", ".join(f"{t}({c})" for t, c in top)
        lines.append(f"| {comp} | {n} | {own / n:.0%} | {neg} | {top_s} |")
    lines.append("")
    for comp, items in data.items():
        lines.append(f"## {comp}")
        negs = [i for i in items if i["tone"] == "부정"]
        if negs:
            lines.append("**대응 검토 필요 (부정 논조)**")
            for i in negs:
                lines.append(f"- {i['date']} {i['source']} | {i['title']}")
        by_topic = defaultdict(list)
        for i in items:
            by_topic[i["topic"]].append(i)
        for topic, its in sorted(by_topic.items(), key=lambda x: -len(x[1])):
            lines.append(f"**{topic} ({len(its)})**")
            for i in its[:8]:
                lines.append(f"- {i['date']} {i['source']} [{i['subject']}] | {i['title']}")
        lines.append("")
    return "\n".join(lines)


def llm_summary(brief: str) -> str:
    try:
        import anthropic  # type: ignore
    except ImportError:
        return "(anthropic 패키지 미설치: pip install anthropic)"
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=600,
        messages=[{
            "role": "user",
            "content": "다음은 국산 AI 반도체 3사의 보도 브리프다. 커뮤니케이션 책임자가 오늘 아침 읽을 "
                       "5줄 요약을 써라. 1) 오늘 대응이 필요한 기사 2) 경쟁사 대비 프레임 변화 3) 다음 주 "
                       "선제 발표 기회. 각 줄은 한 문장.\n\n" + brief,
        }],
    )
    return msg.content[0].text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--json", action="store_true", help="also dump raw JSON")
    ap.add_argument("--from-file", default=None, help="offline: classify headlines from a markdown file instead of fetching")
    args = ap.parse_args()

    if args.from_file:
        data = load_from_file(args.from_file)
    else:
        data = {comp: dedupe(fetch_rss(names[0], args.days)) for comp, names in COMPANIES.items()}
    for comp, items in data.items():
        for i in items:
            i["topic"] = classify_topic(i["title"])
            i["subject"] = classify_subject(i["title"], comp)
            i["tone"] = classify_tone(i["title"])

    brief = build_brief(data, args.days)
    if args.llm and os.environ.get("ANTHROPIC_API_KEY"):
        brief = "## 오늘의 5줄\n" + llm_summary(brief) + "\n\n" + brief

    out = args.out or f"brief_{dt.date.today().isoformat()}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(brief)
    if args.json:
        with open(out.replace(".md", ".json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"written: {out}")
    print(brief[:1500])


if __name__ == "__main__":
    sys.exit(main())
