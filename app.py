"""Speak Up by JAZ Labs - бір Python файлындағы нұсқа (Streamlit + Supabase + Groq).

Іске қосу:  streamlit run app.py
Кілттер .streamlit/secrets.toml ішінде (немесе орта айнымалыларында) сақталады.
"""
import io
import json
import os
import re
import time
import wave
from collections import Counter
from datetime import date, datetime, timedelta

import streamlit as st

st.set_page_config(page_title="Speak Up — JAZ Labs", page_icon="🎙️", layout="wide")


# ----------------------------------------------------------------------------
# 1. Баптаулар (құпия кілттер тек серверде, браузерге берілмейді)
# ----------------------------------------------------------------------------
def cfg(name: str, default: str = "") -> str:
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return str(value or os.environ.get(name, default))


SUPABASE_URL = cfg("SUPABASE_URL")
SUPABASE_ANON_KEY = cfg("SUPABASE_ANON_KEY")
GROQ_API_KEY = cfg("GROQ_API_KEY")
CHAT_MODEL = cfg("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
STT_MODEL = cfg("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo")

LEVELS = ["A1–A2", "B1–B2"]
CATEGORIES = ["Grammar", "Vocabulary", "Verb tense", "Word order", "Articles & prepositions", "Sentence structure", "Other"]
TOPICS = [
    ("Daily Life", "A perfect weekend", "Describe what your perfect weekend would look like."),
    ("Education", "Learning something new", "Talk about a skill you would like to learn and why."),
    ("Technology", "Technology in our lives", "How has technology changed the way people communicate?"),
    ("Travel", "A memorable place", "Describe a place you would really like to visit."),
    ("Hobbies", "Free-time activities", "What do you enjoy doing in your free time?"),
    ("Environment", "Protecting nature", "What can young people do to protect the environment?"),
    ("Society", "Helping others", "Why is it important to help people in your community?"),
    ("Personal Experiences", "A proud moment", "Describe an achievement that made you proud."),
    ("Future Plans", "Your future", "What would you like your life to look like in five years?"),
]
PAGES = ["Home", "Speaking Practice", "AI Assessment", "AI Conversation", "My Progress", "Vocabulary Bank",
         "Topic Explorer", "Daily Challenge", "Personal Goals", "Error Tracker", "Profile"]

st.markdown(
    """
<style>
.hero{background:linear-gradient(110deg,#202e65,#44458f 57%,#7667c4);color:#fff;border-radius:22px;padding:34px 40px;margin-bottom:18px}
.hero h1{margin:0;font-size:36px;line-height:1.2}.hero em{color:#c9c4ff;font-style:normal}
.hero p{color:#d1d5f2;max-width:520px}
.card{background:#fff;border:1px solid #edf0f6;border-radius:14px;padding:14px 16px;margin-bottom:10px}
.bad{color:#c16c75;text-decoration:line-through}.good{color:#368566;font-weight:600}
</style>
""",
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# 2. Көмекші функциялар (барлық сандар шынайы сақталған деректерден есептеледі)
# ----------------------------------------------------------------------------
def friendly(e: Exception) -> str:
    text = str(e) or e.__class__.__name__
    code = getattr(e, "status_code", None)
    if code == 429 or "rate limit" in text.lower():
        return "Тегін AI қызметі бос емес немесе тегін шегі бітті. Бір минуттан кейін қайталаңыз."
    if code in (401, 403):
        return "Қызмет кілтті қабылдамады. Кілтті тексеріңіз."
    if "Invalid login credentials" in text:
        return "Email немесе құпия сөз қате."
    if "Email not confirmed" in text:
        return "Алдымен email-ді растаңыз (поштаңыздағы сілтемені ашыңыз)."
    return text


def word_count(text: str) -> int:
    return len(text.split())


def metrics(text: str, seconds: int):
    words = word_count(text)
    return words, (round(words / (seconds / 60)) if seconds > 0 else 0)  # 0 = белгісіз


def uses_word(text: str, word: str) -> bool:
    word = word.strip()
    if not word:
        return False
    return re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text, re.IGNORECASE) is not None


def fillers(text: str):
    found = re.findall(r"\b(um+|uh+|er+|erm|you know)\b", text, re.IGNORECASE)
    return Counter(x.lower() for x in found)


def wav_seconds(data: bytes) -> int:
    try:
        with wave.open(io.BytesIO(data)) as w:
            return max(1, round(w.getnframes() / float(w.getframerate())))
    except Exception:
        return 0


def throttle(name: str, limit: int = 20, window: int = 600) -> bool:
    now = time.time()
    hits = [t for t in st.session_state.get("hits_" + name, []) if now - t < window]
    if len(hits) >= limit:
        st.warning("Сіз тым жылдам жұмыс істеп жатырсыз. Бір минут күтіп, қайталаңыз.")
        return False
    hits.append(now)
    st.session_state["hits_" + name] = hits
    return True


# ----------------------------------------------------------------------------
# 3. Supabase: тіркелу, кіру, деректер (Row Level Security арқылы қорғалған)
# ----------------------------------------------------------------------------
def client():
    if "sb" not in st.session_state:
        from supabase import create_client

        st.session_state.sb = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return st.session_state.sb


def sign_up(email: str, password: str, name: str):
    res = client().auth.sign_up({"email": email, "password": password, "options": {"data": {"display_name": name}}})
    if res.user is not None and getattr(res.user, "identities", None) == []:
        raise ValueError("Бұл email-мен аккаунт бұрыннан бар болуы мүмкін. Кіріп көріңіз.")
    return res.session is None  # True = email растау керек


def sign_in(email: str, password: str):
    res = client().auth.sign_in_with_password({"email": email, "password": password})
    st.session_state.user = {"id": res.user.id, "email": res.user.email}


def sign_out():
    try:
        client().auth.sign_out()
    finally:
        for k in list(st.session_state.keys()):
            del st.session_state[k]


def table(name: str):
    return client().table(name)


def load_all():
    uid = st.session_state.user["id"]
    prof = table("profiles").select("display_name,english_level").eq("id", uid).limit(1).execute().data
    if not prof:
        table("profiles").upsert({"id": uid}).execute()
        prof = [{"display_name": "Learner", "english_level": "A1–A2"}]
    sessions = table("speaking_sessions").select("*, assessments(result)").order("created_at", desc=True).limit(500).execute().data
    for s in sessions:
        a = s.get("assessments")
        a = a[0] if isinstance(a, list) and a else (a if isinstance(a, dict) else None)
        s["assessment"] = a["result"] if a else None
    done = table("daily_challenges").select("completed").eq("challenge_date", date.today().isoformat()).limit(1).execute().data
    return {
        "profile": {"name": prof[0]["display_name"] or "Learner", "level": prof[0]["english_level"] or "A1–A2"},
        "sessions": sessions,
        "words": table("vocabulary").select("*").order("created_at", desc=True).execute().data,
        "goals": table("goals").select("*").order("created_at").execute().data,
        "recs": table("learning_recommendations").select("*").order("created_at", desc=True).limit(20).execute().data,
        "challenge_done": bool(done and done[0]["completed"]),
    }


def D():
    if "data" not in st.session_state:
        st.session_state.data = load_all()
    return st.session_state.data


def refresh():
    st.session_state.pop("data", None)


def safe(fn, ok: str = ""):
    """Әрекетті орындайды, қате болса түсінікті хабар көрсетеді."""
    try:
        result = fn()
        if ok:
            st.toast(ok)
        return True, result
    except Exception as e:  # noqa: BLE001
        st.error(friendly(e))
        return False, None


# ----------------------------------------------------------------------------
# 4. Нақты AI: Groq (тегін) - Speech-to-Text, бағалау, әңгімелесу, ұсыныстар
# ----------------------------------------------------------------------------
def groq():
    if not GROQ_API_KEY:
        raise RuntimeError("AI қосылмаған: GROQ_API_KEY кілтін secrets.toml файлына қосыңыз.")
    from groq import Groq

    return Groq(api_key=GROQ_API_KEY)


def transcribe(data: bytes, filename: str) -> str:
    if len(data) < 1000:
        raise ValueError("Жазба тым қысқа немесе бос. Қайта жазыңыз.")
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Файл 20 МБ-тан үлкен.")
    out = groq().audio.transcriptions.create(file=(filename, data), model=STT_MODEL, language="en", response_format="json")
    text = (out.text or "").strip()
    if not text:
        raise ValueError("Сөйлеу анықталмады. Микрофонға жақынырақ сөйлеп, қайта жазыңыз.")
    return text


def chat_json(system: str, payload: dict) -> dict:
    out = groq().chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.3,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
    )
    return json.loads(out.choices[0].message.content or "{}")


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s).lower()).strip()


def assess(question: str, answer: str, level: str) -> dict:
    if word_count(answer) < 3:
        raise ValueError("Жауап бағалау үшін тым қысқа. Сәл көбірек айтыңыз.")
    raw = chat_json(
        f"You are a supportive English speaking tutor for school-age learners at CEFR level {level}. Return ONLY valid JSON with keys: "
        "overall (string, 1-2 sentences), strengths (string array), improvements (string array), corrections (array of {original, corrected, explanation}), "
        "naturalVersion (string), nextSteps (string array, concrete practice actions), errors (array of {category, original, correction}). "
        "Assess only grammar, vocabulary, structure, relevance, development and clarity of the TRANSCRIPT. Do not give scores or an IELTS band. "
        "Never comment on pronunciation, accent, pauses or delivery because you only see text. "
        'In corrections and errors, "original" must be copied EXACTLY from the learner\'s answer (a short phrase). '
        f"category must be one of: {', '.join(CATEGORIES)}. If there are no clear mistakes return empty corrections and errors. Be kind and age appropriate.",
        {"question": question, "answer": answer, "level": level},
    )
    if not isinstance(raw.get("overall"), str) or not raw["overall"].strip():
        raise ValueError("AI күтпеген жауап қайтарды. Қайталап көріңіз.")
    text = norm(answer)

    def real(item, a, b):  # тек жауапта шынымен бар қателерді қалдырамыз
        o = norm(item.get(a, ""))
        return bool(o) and o in text and o != norm(item.get(b, ""))

    cats = {norm(c): c for c in CATEGORIES}
    lists = lambda k: [str(x) for x in raw.get(k, []) if isinstance(x, str)]  # noqa: E731
    return {
        "overall": raw["overall"],
        "strengths": lists("strengths"),
        "improvements": lists("improvements"),
        "corrections": [c for c in raw.get("corrections", []) if isinstance(c, dict) and real(c, "original", "corrected")],
        "naturalVersion": str(raw.get("naturalVersion", "")),
        "nextSteps": lists("nextSteps"),
        "errors": [
            {**e, "category": cats.get(norm(e.get("category", "")), "Other")}
            for e in raw.get("errors", [])
            if isinstance(e, dict) and real(e, "original", "correction")
        ],
    }


def converse(messages: list, level: str) -> str:
    out = groq().chat.completions.create(
        model=CHAT_MODEL,
        temperature=0.7,
        max_tokens=300,
        messages=[
            {
                "role": "system",
                "content": f"You are a warm, age-appropriate English conversation partner for a learner at CEFR level {level}. Adapt vocabulary and sentence length to that level. "
                "Reply in 1-4 short sentences, ask one useful follow-up question, and gently model a better phrase when the learner makes a mistake. "
                "Never shame the learner. Stay on friendly, school-safe topics.",
            },
            *messages[-20:],
        ],
    )
    reply = (out.choices[0].message.content or "").strip()
    if not reply:
        raise ValueError("AI бос жауап берді. Қайталап көріңіз.")
    return reply


# ----------------------------------------------------------------------------
# 5. Статистика, прогресс және қателер (тек нақты деректерден)
# ----------------------------------------------------------------------------
def stats(d: dict) -> dict:
    sessions = d["sessions"]
    timed = [s for s in sessions if s["wpm"] > 0]
    assessed = [s for s in sessions if s["assessment"]]
    days = [(date.today() - timedelta(days=6 - i)) for i in range(7)]
    per_day = Counter(datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")).astimezone().date() for s in sessions)
    active = set(per_day)
    cur, streak = date.today(), 0
    if cur not in active:
        cur -= timedelta(days=1)
    while cur in active:
        streak += 1
        cur -= timedelta(days=1)
    errors = [{**e, "topic": s["topic"], "date": s["created_at"][:10]} for s in assessed for e in s["assessment"].get("errors", [])]
    fill = Counter()
    for s in sessions:
        fill.update(fillers(s["transcript"]))
    return {
        "sessions": len(sessions),
        "words": sum(s["word_count"] for s in sessions),
        "pace": round(sum(s["wpm"] for s in timed) / len(timed)) if timed else 0,
        "assessed": len(assessed),
        "last7": sum(per_day[x] for x in days),
        "avg_words": round(sum(s["word_count"] for s in sessions) / len(sessions)) if sessions else 0,
        "days": {x.strftime("%a"): per_day[x] for x in days},
        "streak": streak,
        "errors": errors,
        "cats": Counter(e["category"] for e in errors).most_common(),
        "fillers": fill.most_common(),
        "unlearned": sum(1 for w in d["words"] if not w["learned"]),
        "activated": sum(1 for w in d["words"] if w.get("activated_at")),
    }


def daily_topic():
    return TOPICS[date.today().day % len(TOPICS)]


# ----------------------------------------------------------------------------
# 6. Беттер
# ----------------------------------------------------------------------------
def page_home(d):
    s = stats(d)
    st.markdown('<div class="hero"><h1>Find your voice.<br><em>Speak with confidence.</em></h1><p>A little practice every day can change the way you express yourself.</p></div>', unsafe_allow_html=True)
    st.caption(f"Level: {d['profile']['level']} · деректер — сіздің сақталған жаттығуларыңыздан")
    c = st.columns(4)
    c[0].metric("Speaking sessions", s["sessions"], f"{s['streak']}-day streak" if s["streak"] else None)
    c[1].metric("Words spoken", f"{s['words']:,}")
    c[2].metric("Average pace", f"{s['pace']} WPM" if s["pace"] else "—")
    c[3].metric("Saved vocabulary", len(d["words"]))
    st.subheader("Your goals")
    if not d["goals"]:
        st.info("Әзірге мақсат жоқ. «Personal Goals» бетінде жасаңыз.")
    for g in d["goals"][:3]:
        st.write(f"**{g['title']}** — {g['current']} / {g['target']} {g['unit']}")
        st.progress(min(1.0, g["current"] / g["target"]))


def page_practice(d):
    st.title("Your voice, your progress.")
    if st.session_state.pop("_clear", False):
        st.session_state["transcript"] = ""
        st.session_state["duration"] = 0
        st.session_state["rec_n"] = st.session_state.get("rec_n", 0) + 1
    titles = [t[1] for t in TOPICS]
    if "topic_pick" not in st.session_state:
        st.session_state["topic_pick"] = titles[0]
    topic = TOPICS[titles.index(st.session_state["topic_pick"])]
    left, right = st.columns(2)
    with left:
        st.subheader("1. Choose your topic")
        st.selectbox("Topic", titles, key="topic_pick")
        topic = TOPICS[titles.index(st.session_state["topic_pick"])]
        st.info(f"**{topic[0]}** — {topic[2]}")
        st.caption("Try 3–5 simple sentences." if d["profile"]["level"] == "A1–A2" else "Develop your answer with reasons, examples and a conclusion.")
        st.subheader("2. Record your answer")
        rec = st.audio_input("Микрофонды басып сөйлеңіз", key=f"rec{st.session_state.get('rec_n', 0)}")
        up = st.file_uploader("…немесе аудио файл жүктеңіз", type=["wav", "mp3", "m4a", "ogg", "webm", "flac"])
        source = rec or up
        if source is not None and st.button("Transcribe audio", type="primary"):
            if throttle("stt"):
                data = source.getvalue()
                ok, text = safe(lambda: transcribe(data, source.name or "recording.wav"))
                if ok:
                    st.session_state["transcript"] = text
                    st.session_state["duration"] = wav_seconds(data)
                    st.toast("Transcript received. Review it before saving.")
    with right:
        st.subheader("3. Review your transcript")
        transcript = st.text_area("Transcript", key="transcript", height=260, max_chars=6000, placeholder="Транскрипт осында шығады. Қолмен де жазуға болады.")
        duration = st.session_state.get("duration", 0)
        words, _ = metrics(transcript, duration)
        st.caption(f"{words} words · {duration // 60}m {duration % 60}s recorded" if duration else f"{words} words · жазба ұзақтығы белгісіз")
        if st.button("Save practice session"):
            if not transcript.strip():
                st.error("Алдымен жауабыңызды жазыңыз немесе транскрипция жасаңыз.")
            else:
                save_session(d, topic, transcript.strip(), duration)
        st.caption("Жазба сақталмайды: ол тек «Transcribe» басқанда сөйлеуді мәтінге айналдыратын қызметке жіберіледі.")


def save_session(d, topic, transcript, duration):
    words, wpm = metrics(transcript, duration)
    uid = st.session_state.user["id"]
    ok, _ = safe(lambda: table("speaking_sessions").insert({"user_id": uid, "topic": topic[1], "question": topic[2], "transcript": transcript, "duration_seconds": duration, "word_count": words, "wpm": wpm}).execute())
    if not ok:
        return
    notes = []
    try:  # сессия сақталды; қалғаны қосымша жаңарту, қате болса да жауап жоғалмайды
        ids = [w["id"] for w in d["words"] if not w.get("activated_at") and uses_word(transcript, w["word"])]
        if ids:
            table("vocabulary").update({"activated_at": datetime.utcnow().isoformat()}).in_("id", ids).execute()
            notes.append(f"{len(ids)} сақталған сөз қолданылды")
        for g in d["goals"]:
            if g["unit"] == "sessions" and g["current"] < g["target"]:
                table("goals").update({"current": g["current"] + 1}).eq("id", g["id"]).execute()
        if topic[1] == daily_topic()[1] and not d["challenge_done"]:
            table("daily_challenges").upsert({"user_id": uid, "challenge_date": date.today().isoformat(), "prompt": topic[2], "completed": True, "completed_at": datetime.utcnow().isoformat()}, on_conflict="user_id,challenge_date").execute()
            notes.append("күнделікті тапсырма орындалды")
    except Exception as e:  # noqa: BLE001
        st.warning(f"Сессия сақталды, бірақ кейбір жаңартулар орындалмады: {friendly(e)}")
    refresh()
    st.session_state["_clear"] = True
    st.toast("Practice session saved." + (f" ({', '.join(notes)})" if notes else ""))
    st.rerun()


def show_assessment(a):
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"### {a['overall']}")
        st.write("**What you did well**")
        for x in a.get("strengths", []):
            st.write(f"- {x}")
        st.write("**Areas to improve**")
        for x in a.get("improvements", []):
            st.write(f"- {x}")
    with c2:
        st.write("**Corrections**")
        if not a.get("corrections"):
            st.caption("Бұл жауапта нақты қате табылмады.")
        for c in a.get("corrections", []):
            st.markdown(f'<div class="card"><span class="bad">{c["original"]}</span> → <span class="good">{c["corrected"]}</span><br><small>{c.get("explanation", "")}</small></div>', unsafe_allow_html=True)
        if a.get("naturalVersion"):
            st.write("**More natural version**")
            st.write(a["naturalVersion"])
        st.write("**Next steps**")
        for x in a.get("nextSteps", []):
            st.write(f"- {x}")
    st.caption("Бұл оқу кері байланысы: ресми IELTS балл емес және айтылымды (pronunciation) бағаламайды.")


def page_assessment(d):
    st.title("Understand your speaking.")
    if not d["sessions"]:
        return st.info("Алдымен «Speaking Practice» бетінде жауап сақтаңыз.")
    labels = {s["id"]: f"{s['topic']} · {s['created_at'][:10]}{' · assessed' if s['assessment'] else ''}" for s in d["sessions"]}
    sid = st.selectbox("Select a practice answer", list(labels), format_func=labels.get)
    s = next(x for x in d["sessions"] if x["id"] == sid)
    st.write(f"**Question:** {s['question']}")
    st.write(f"**Your answer:** {s['transcript']}")
    if st.button("Analyze again" if s["assessment"] else "Analyze my answer", type="primary") and throttle("ai", 30):
        with st.spinner("Analyzing…"):
            ok, a = safe(lambda: assess(s["question"], s["transcript"], d["profile"]["level"]))
        if ok:
            uid = st.session_state.user["id"]
            saved, _ = safe(lambda: table("assessments").upsert({"user_id": uid, "session_id": s["id"], "result": a}, on_conflict="session_id").execute(), "AI assessment completed and saved.")
            s["assessment"] = a
            if saved:
                refresh()
    if s["assessment"]:
        show_assessment(s["assessment"])


def page_conversation(d):
    st.title("Let's talk.")
    level = d["profile"]["level"]
    if "chat" not in st.session_state:
        rows = table("conversation_messages").select("role,content").order("created_at", desc=True).limit(60).execute().data
        st.session_state.chat = list(reversed(rows))
    greeting = {"role": "assistant", "content": "Hi! I'm your English practice partner. What do you like doing after school?" if level == "A1–A2" else "Hello! Let's have a natural conversation. What is one skill you think every student should develop, and why?"}
    chat = st.session_state.chat
    uid = st.session_state.user["id"]
    if st.button("New conversation", disabled=not chat):
        ok, _ = safe(lambda: table("conversation_messages").delete().eq("user_id", uid).execute())
        if ok:
            st.session_state.chat = []
            st.rerun()
    for m in [greeting, *chat]:
        with st.chat_message(m["role"]):
            st.write(m["content"])

    def send(text: str):
        if not throttle("ai", 30):
            return
        msg = {"role": "user", "content": text.strip()[:1000]}
        chat.append(msg)
        safe(lambda: table("conversation_messages").insert({"user_id": uid, **msg}).execute())
        with st.spinner("Thinking of a reply…"):
            ok, reply = safe(lambda: converse([greeting, *chat], level))
        if ok:
            ans = {"role": "assistant", "content": reply}
            chat.append(ans)
            safe(lambda: table("conversation_messages").insert({"user_id": uid, **ans}).execute())
        st.rerun()

    with st.expander("🎤 Reply by voice"):
        rec = st.audio_input("Record your reply", key=f"chatrec{len(chat)}")
        if rec is not None and st.button("Transcribe and send") and throttle("stt"):
            ok, text = safe(lambda: transcribe(rec.getvalue(), rec.name or "reply.wav"))
            if ok:
                send(text)
    text = st.chat_input("Write your message in English…")
    if text and text.strip():
        send(text)


def page_progress(d):
    st.title("Every practice counts.")
    s = stats(d)
    a = st.columns(4)
    a[0].metric("Total sessions", s["sessions"])
    a[1].metric("Words spoken", s["words"])
    a[2].metric("Average pace", f"{s['pace']} WPM" if s["pace"] else "—")
    a[3].metric("Vocabulary", len(d["words"]))
    b = st.columns(4)
    b[0].metric("Practice streak", f"{s['streak']} days")
    b[1].metric("Assessed answers", s["assessed"])
    b[2].metric("Words put to use", s["activated"])
    b[3].metric("Average answer", f"{s['avg_words']} words")
    st.subheader("Last seven days")
    if d["sessions"]:
        st.bar_chart(s["days"])
    else:
        st.info("Алғашқы жаттығуды сақтаңыз — график осында шығады.")
    st.subheader("Personalized recommendations")
    st.caption("AI жазады — тек сіздің сақталған бағалауларыңыз бен статистикаңыздан.")
    if s["assessed"] < 1:
        st.info("Алдымен кем дегенде бір жауапты AI арқылы бағалаңыз.")
    elif st.button("Generate", type="primary") and throttle("ai", 30):
        payload = {
            "sessions": s["sessions"], "assessedSessions": s["assessed"], "sessionsLast7Days": s["last7"], "averageWpm": s["pace"],
            "averageWords": s["avg_words"], "unlearnedWords": s["unlearned"],
            "topErrorCategories": [{"category": c, "count": n} for c, n in s["cats"][:5]],
            "recentCorrections": [{"original": e["original"][:200], "correction": e["correction"][:200], "category": e["category"]} for e in s["errors"][:10]],
        }
        with st.spinner("Thinking…"):
            ok, raw = safe(lambda: chat_json(
                f"You are an English speaking coach for a learner at CEFR level {d['profile']['level']}. Using ONLY the JSON data provided, write 2 or 3 personalised practice recommendations. "
                'Return ONLY JSON: {"recommendations":[{"title": string (max 60 chars), "description": string (1-2 sentences with one concrete action)}]}. '
                "Refer to the learner's actual error categories or corrections when they exist. Do not invent statistics or facts not in the data. Do not mention pronunciation.", payload))
        if ok:
            items = [r for r in raw.get("recommendations", []) if isinstance(r, dict) and r.get("title") and r.get("description")][:3]
            if not items:
                st.error("AI күтпеген жауап қайтарды. Қайталап көріңіз.")
            else:
                uid = st.session_state.user["id"]
                saved, _ = safe(lambda: table("learning_recommendations").insert([{"user_id": uid, "title": str(r["title"])[:120], "description": str(r["description"])[:500]} for r in items]).execute(), "New recommendations created.")
                if saved:
                    refresh()
                    st.rerun()
    for r in d["recs"]:
        done = st.checkbox(f"**{r['title']}** — {r['description']}", value=r["completed"], key=f"rec_{r['id']}")
        if done != r["completed"]:
            ok, _ = safe(lambda: table("learning_recommendations").update({"completed": done}).eq("id", r["id"]).execute())
            if ok:
                refresh()
                st.rerun()
    if s["cats"] or s["fillers"]:
        st.subheader("What your data shows")
        if s["cats"]:
            st.write("Mistake types: " + ", ".join(f"{c} ({n})" for c, n in s["cats"]))
        if s["fillers"]:
            st.write("Hesitation sounds: " + ", ".join(f"{w} ({n})" for w, n in s["fillers"]))
    st.subheader("Recent practice")
    for x in d["sessions"][:6]:
        st.write(f"**{x['topic']}** · {x['created_at'][:16].replace('T', ' ')} · {x['word_count']} words · {str(x['wpm']) + ' WPM' if x['wpm'] else '— WPM'}")


def page_vocab(d):
    st.title("Words worth keeping.")
    uid = st.session_state.user["id"]
    with st.form("addword", clear_on_submit=True):
        c = st.columns(2)
        word = c[0].text_input("English word or phrase", max_chars=200)
        tr = c[1].text_input("Translation / meaning", max_chars=200)
        ex = st.text_input("Example sentence (optional)", max_chars=300)
        if st.form_submit_button("Save word", type="primary") and word.strip():
            if any(w["word"].lower() == word.strip().lower() for w in d["words"]):
                st.warning("Бұл сөз тізімде бар.")
            else:
                ok, _ = safe(lambda: table("vocabulary").insert({"user_id": uid, "word": word.strip(), "translation": tr.strip(), "example": ex.strip()}).execute(), "Word saved.")
                if ok:
                    refresh()
                    st.rerun()
    q = st.text_input("Search words…").lower()
    shown = [w for w in d["words"] if q in (w["word"] + " " + (w["translation"] or "")).lower()]
    st.caption(f"{len(d['words'])} words")
    if not shown:
        st.info("Сөз табылмады.")
    for w in shown:
        c = st.columns([6, 2, 1, 1])
        tags = (" · 🗣 used in speech" if w.get("activated_at") else "") + (" · ✅ learned" if w["learned"] else "")
        c[0].markdown(f"**{w['word']}** — {w['translation'] or ''}{tags}" + (f"  \n_“{w['example']}”_" if w["example"] else ""))
        if c[2].button("✔", key=f"l{w['id']}", help="Mark learned"):
            safe(lambda: table("vocabulary").update({"learned": not w["learned"]}).eq("id", w["id"]).execute())
            refresh()
            st.rerun()
        if c[3].button("🗑", key=f"d{w['id']}", help="Delete"):
            safe(lambda: table("vocabulary").delete().eq("id", w["id"]).execute())
            refresh()
            st.rerun()


def pick_topic(title: str):
    st.session_state["topic_pick"] = title
    st.session_state["page"] = "Speaking Practice"


def page_topics(d):
    st.title("Find something to talk about.")
    cols = st.columns(3)
    for i, (cat, title, q) in enumerate(TOPICS):
        with cols[i % 3]:
            st.markdown(f'<div class="card"><small>{cat.upper()}</small><br><b>{title}</b><br>{q}</div>', unsafe_allow_html=True)
            st.button("Practise this topic →", key=f"t{i}", on_click=pick_topic, args=(title,))


def page_challenge(d):
    st.title("One prompt. One step forward.")
    cat, title, q = daily_topic()
    st.subheader(q)
    st.write("Ашылуы, екі қолдау бөлігі және қысқа қорытынды қосыңыз. Осы тақырыпта жаттығу сақтасаңыз, тапсырма өздігінен орындалады.")
    st.button("Start this challenge →", on_click=pick_topic, args=(title,), type="primary")
    if d["challenge_done"]:
        st.success("Completed today ✔")
    elif st.button("Mark as completed"):
        uid = st.session_state.user["id"]
        ok, _ = safe(lambda: table("daily_challenges").upsert({"user_id": uid, "challenge_date": date.today().isoformat(), "prompt": q, "completed": True, "completed_at": datetime.utcnow().isoformat()}, on_conflict="user_id,challenge_date").execute(), "Challenge marked complete.")
        if ok:
            refresh()
            st.rerun()


def page_goals(d):
    st.title("Make your practice intentional.")
    uid = st.session_state.user["id"]
    with st.form("addgoal", clear_on_submit=True):
        title = st.text_input("Goal", max_chars=200, placeholder="e.g. Practise speaking every week")
        c = st.columns(2)
        target = c[0].number_input("Target", min_value=1, max_value=10000, value=5, step=1)
        unit = c[1].selectbox("Type", ["sessions", "steps"], format_func=lambda x: "Speaking sessions (автоматты)" if x == "sessions" else "Custom steps (өзім жаңартамын)")
        if st.form_submit_button("Add goal", type="primary") and title.strip():
            ok, _ = safe(lambda: table("goals").insert({"user_id": uid, "title": title.strip(), "target": int(target), "unit": unit}).execute(), "Goal added.")
            if ok:
                refresh()
                st.rerun()
    if not d["goals"]:
        st.info("Әзірге мақсат жоқ.")
    for g in d["goals"]:
        c = st.columns([6, 2, 1])
        c[0].write(f"**{g['title']}** — {g['current']} / {g['target']} {g['unit']}")
        c[0].progress(min(1.0, g["current"] / g["target"]))
        if c[1].button("+1 progress", key=f"p{g['id']}", disabled=g["current"] >= g["target"]):
            safe(lambda: table("goals").update({"current": min(g["target"], g["current"] + 1)}).eq("id", g["id"]).execute())
            refresh()
            st.rerun()
        if c[2].button("🗑", key=f"gd{g['id']}"):
            safe(lambda: table("goals").delete().eq("id", g["id"]).execute())
            refresh()
            st.rerun()


def page_errors(d):
    st.title("Learn from patterns, not perfection.")
    s = stats(d)
    if not s["errors"]:
        return st.info("AI бағалауы әлі жоқ. Сақталған жауапты бағалаңыз — түзетулер осында шығады.")
    st.write("**Mistake types:** " + ", ".join(f"{c} ({n})" for c, n in s["cats"]))
    seen = Counter((e["original"].lower(), e["correction"].lower()) for e in s["errors"])
    repeated = [(k, n) for k, n in seen.most_common() if n > 1]
    if repeated:
        st.subheader("Repeated mistakes")
        for (o, c), n in repeated:
            st.write(f"{o} → **{c}** (seen {n} times)")
    st.subheader("All identified errors")
    for e in s["errors"]:
        st.markdown(f'<div class="card"><small>{e["category"]}</small><br><span class="bad">{e["original"]}</span> → <span class="good">{e["correction"]}</span><br><small>{e["topic"]} · {e["date"]}</small></div>', unsafe_allow_html=True)


def page_profile(d):
    st.title("Your learning profile.")
    uid = st.session_state.user["id"]
    name = st.text_input("Your name", value=d["profile"]["name"], max_chars=80)
    st.text_input("Email", value=st.session_state.user["email"], disabled=True)
    level = st.selectbox("English level", LEVELS, index=LEVELS.index(d["profile"]["level"]) if d["profile"]["level"] in LEVELS else 0)
    if st.button("Save profile", type="primary"):
        ok, _ = safe(lambda: table("profiles").upsert({"id": uid, "display_name": name.strip() or "Learner", "english_level": level}).execute(), "Profile saved.")
        if ok:
            refresh()
            st.rerun()
    st.caption("Деректеріңіз тек өз аккаунтыңызға көрінеді (Row Level Security).")


# ----------------------------------------------------------------------------
# 7. Кіру / тіркелу беті және негізгі бағдарлама
# ----------------------------------------------------------------------------
def auth_page():
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        st.title("🎙️ Speak Up")
        st.caption("by JAZ Labs")
        t_in, t_up = st.tabs(["Sign in", "Create account"])
        with t_in:
            with st.form("signin"):
                email = st.text_input("Email")
                pw = st.text_input("Password", type="password")
                if st.form_submit_button("Sign in", type="primary"):
                    ok, _ = safe(lambda: sign_in(email.strip(), pw))
                    if ok:
                        st.rerun()
        with t_up:
            with st.form("signup"):
                name = st.text_input("Your name", max_chars=80)
                email = st.text_input("Email", key="su_email")
                pw = st.text_input("Password (кемінде 8 таңба)", type="password", key="su_pw")
                if st.form_submit_button("Create account", type="primary"):
                    if not name.strip() or "@" not in email or len(pw) < 8:
                        st.error("Атыңызды, дұрыс email және кемінде 8 таңбалы құпия сөз енгізіңіз.")
                    else:
                        ok, confirm = safe(lambda: sign_up(email.strip(), pw, name.strip()))
                        if ok and confirm:
                            st.success(f"Аккаунт жасалды. {email} поштасына растау сілтемесі жіберілді — ашып, кейін кіріңіз.")
                        elif ok:
                            st.rerun()
        st.caption("Жазбалар сақталмайды. Деректер тек сіздің аккаунтыңызға тиесілі.")


def main():
    missing = [n for n, v in [("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY)] if not v]
    if missing:
        st.error("Supabase кілттері қойылмаған: " + ", ".join(missing) + ". `.streamlit/secrets.toml` файлын README бойынша толтырыңыз.")
        st.stop()
    if "user" not in st.session_state:
        return auth_page()
    ok, d = safe(D)
    if not ok:
        st.info("Supabase SQL файлын (`supabase/setup-all.sql`) іске қостыңыз ба? Тексеріп, бетті жаңартыңыз.")
        if st.button("Sign out"):
            sign_out()
            st.rerun()
        return
    if not GROQ_API_KEY:
        st.warning("AI қосылмаған (GROQ_API_KEY жоқ): транскрипция, бағалау және әңгіме жұмыс істемейді.")
    with st.sidebar:
        st.markdown("## 🎙️ Speak Up\nby JAZ Labs")
        st.radio("Menu", PAGES, key="page", label_visibility="collapsed")
        st.caption(f"👤 {d['profile']['name']}")
        if st.button("Sign out"):
            sign_out()
            st.rerun()
    handlers = {
        "Home": page_home, "Speaking Practice": page_practice, "AI Assessment": page_assessment, "AI Conversation": page_conversation,
        "My Progress": page_progress, "Vocabulary Bank": page_vocab, "Topic Explorer": page_topics, "Daily Challenge": page_challenge,
        "Personal Goals": page_goals, "Error Tracker": page_errors, "Profile": page_profile,
    }
    handlers[st.session_state.get("page", "Home")](d)


main()
