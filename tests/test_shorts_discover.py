"""YT Shorts "Find channels": search, check, filter, wishlist.

YouTube is replaced with a small fake catalogue so each rule can be checked
exactly: a channel must really post Shorts, its language comes from its titles,
its country from its own About page, and nothing already saved, dismissed or
shown before is suggested again. (The same code was also run live against
YouTube while building it — see PROGRESS.md.)
"""

import json

import pytest

from shortforge.config import Config
from shortforge.shorts import countries as CO
from shortforge.shorts import discover as D
from shortforge.shorts import langid as LG
from shortforge.shorts import store as S
from shortforge.utils import ShortForgeError


# --- language detection --------------------------------------------------------- #

@pytest.mark.parametrize("titles,code", [
    (["Hyderabadi Red Chicken Recipe #food", "ye kaunsa paneer hai #recipe",
      "10 min wala Milk Cake Recipe 😜"], "hi-Latn"),
    (["How to make the best pizza at home", "This is why you should never do this"], "en"),
    (["آج ہم بنائیں گے مزیدار بریانی", "گھر میں آسان طریقے سے کیک بنائیں"], "ur"),
    (["أفضل طريقة لعمل الكبسة في المنزل", "وصفة سهلة وسريعة للعشاء"], "ar"),
    (["घर पर बनाएं स्वादिष्ट पनीर", "आसान रेसिपी"], "hi"),
    (["La mejor receta de tacos para la cena", "Cómo hacer pan en casa"], "es"),
    (["Cara membuat nasi goreng yang enak banget"], "id"),
    (["오늘의 요리 레시피", "간단한 김치볶음밥"], "ko"),
    (["Как приготовить борщ дома"], "ru"),
])
def test_language_from_titles(titles, code):
    assert LG.detect(titles)[0] == code


def test_hashtags_alone_say_nothing_about_language():
    assert LG.detect(["#shorts #viral #food"]) == ("", 0.0)


def test_roman_hindi_urdu_matches_either_language_but_not_english():
    assert LG.matches("hi-Latn", "ur") and LG.matches("hi-Latn", "hi")
    assert not LG.matches("hi-Latn", "en")
    assert LG.matches("en", "")                       # "any language"


def test_unknown_language_is_labelled_unknown_not_any():
    assert LG.label("") == "unknown"


# --- countries ------------------------------------------------------------------ #

@pytest.mark.parametrize("text,code", [("Pakistan", "PK"), ("United Arab Emirates", "AE"),
                                       ("UAE", "AE"), ("Türkiye", "TR"), ("Turkey", "TR"),
                                       ("United States", "US"), ("pk", "PK"), ("Narnia", "")])
def test_country_names_as_youtube_writes_them(text, code):
    assert CO.code_for(text) == code


def test_about_page_country_is_the_channels_own():
    """YouTube's About page puts the channel's country inside its own block —
    distinct from the region YouTube assumed for the VIEWER (GL)."""
    page = ('<script>var ytInitialData = {"x": {"aboutChannelViewModel": '
            '{"description": "Cooking at home", "country": "Pakistan", '
            '"subscriberCountText": "1.2M subscribers"}}};</script>'
            '<script>ytcfg.set({"GL":"US"})</script>')
    a = D._about_from_html(page)
    assert a["country"] == "Pakistan" and a["description"] == "Cooking at home"
    assert D._about_from_html("<html>nothing</html>")["country"] == ""


# --- planning -------------------------------------------------------------------- #

def test_keywords_skip_filler_and_generic_hashtags():
    assert D.tokens("The best Pakistani cooking recipes #shorts #viral in Urdu") == \
        ["pakistani", "cooking", "recipes", "urdu"]


def test_queries_without_ai_name_the_country_and_language():
    qs = D.plan_queries("pakistani cooking", ["pakistani", "cooking"], [], "PK", "ur",
                        from_reference=False)
    assert qs[0] == "pakistani cooking"
    assert "pakistani cooking Pakistan" in qs and "pakistani cooking Urdu" in qs


def test_ai_reply_is_parsed_even_when_wrapped_in_prose():
    assert D._json_block('Sure!\n```json\n{"queries": ["a b"]}\n```') == {"queries": ["a b"]}
    assert D._json_block("no json here") is None


# --- the search, against a fake YouTube ----------------------------------------- #

def _ch(cid, name, *, titles, country="", subs=10_000, shorts=None, handle=None):
    return {"id": cid, "name": name, "handle": handle or f"@{name.lower().replace(' ', '')}",
            "titles": titles, "country": country, "subs": subs,
            "shorts": len(titles) if shorts is None else shorts}


URDU = ["ghar pe biryani kaise banaye", "ye recipe zaroor try karo", "aloo ki sabzi bahut tasty hai"]
ENGLISH = ["How to make the best biryani", "This recipe is so easy", "What I eat in a day"]

CATALOGUE = {
    "UCpk1aaaaaaaaaaaaaaaaaaa": _ch("UCpk1aaaaaaaaaaaaaaaaaaa", "Lahore Kitchen", titles=URDU, country="Pakistan", subs=500_000),
    "UCpk2aaaaaaaaaaaaaaaaaaa": _ch("UCpk2aaaaaaaaaaaaaaaaaaa", "Karachi Cooks", titles=URDU, country="Pakistan", subs=90_000),
    "UCus1aaaaaaaaaaaaaaaaaaa": _ch("UCus1aaaaaaaaaaaaaaaaaaa", "Texas Biryani", titles=ENGLISH, country="United States"),
    "UCin1aaaaaaaaaaaaaaaaaaa": _ch("UCin1aaaaaaaaaaaaaaaaaaa", "Delhi Dhaba", titles=URDU, country="India"),
    "UCnc1aaaaaaaaaaaaaaaaaaa": _ch("UCnc1aaaaaaaaaaaaaaaaaaa", "Mystery Masala", titles=URDU, country=""),
    "UCns1aaaaaaaaaaaaaaaaaaa": _ch("UCns1aaaaaaaaaaaaaaaaaaa", "Long Videos Only", titles=URDU, country="Pakistan", shorts=0),
    "UCsm1aaaaaaaaaaaaaaaaaaa": _ch("UCsm1aaaaaaaaaaaaaaaaaaa", "Tiny Kitchen PK", titles=URDU, country="Pakistan", subs=40),
}


@pytest.fixture
def fake_yt(monkeypatch, tmp_path):
    dd = str(tmp_path / "data")
    monkeypatch.setattr(S, "data_dir", lambda cfg=None: dd)
    monkeypatch.setattr(D, "ai_available", lambda: False)
    calls = {"about": 0, "searches": []}

    def search_channels(q, cfg, limit=30, lang=""):
        calls["searches"].append(q)
        return [{"id": c["id"], "url": f"https://www.youtube.com/channel/{c['id']}",
                 "name": c["name"], "handle": c["handle"], "subscribers": c["subs"],
                 "description": "home cooking", "avatar": ""} for c in CATALOGUE.values()]

    def listing(url, cfg, limit):
        cid = url.split("/channel/")[1].split("/")[0] if "/channel/" in url else None
        c = CATALOGUE.get(cid)
        if c is None:
            raise ShortForgeError("could not list this channel")
        if c["shorts"] == 0:
            raise ShortForgeError("this channel has no Shorts")
        return {"channel": c["name"], "uploader_id": c["handle"], "channel_id": c["id"],
                "channel_follower_count": c["subs"],
                "entries": [{"id": f"v{i}", "title": t, "view_count": 1000}
                            for i, t in enumerate(c["titles"] * 2)][:limit]}

    def fetch_about(url, cfg):
        calls["about"] += 1
        cid = url.split("/channel/")[1].split("/")[0]
        return {"country": CATALOGUE[cid]["country"], "description": "home cooking"}

    monkeypatch.setattr(D, "search_channels", search_channels)
    monkeypatch.setattr(D, "search_video_channels", lambda q, cfg, limit=30, lang="": [])
    monkeypatch.setattr(D, "featured_channels", lambda url, cfg: [])
    monkeypatch.setattr(D, "_listing", listing)
    monkeypatch.setattr(D, "fetch_about", fetch_about)
    return dd, calls


def _find(dd, **params):
    base = {"mode": "describe", "prompt": "pakistani cooking", "count": 5, "use_ai": False}
    job = D.new_job(dd, {**base, **params})
    return D.run(dd, job["id"], Config.load())


def _names(job):
    return [r["name"] for r in job["results"]]


def test_country_and_language_filters_use_the_channels_own_data(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=False)
    assert job["status"] == "done"
    assert _names(job) == ["Lahore Kitchen", "Karachi Cooks", "Tiny Kitchen PK"]
    rej = job["rejected"]
    assert rej["wrong_country"] == 1          # Delhi Dhaba (India)
    assert rej["wrong_language"] == 1         # Texas Biryani (English)
    assert rej["no_country"] == 1             # Mystery Masala (none listed, strict)
    assert rej["no_shorts"] == 1              # Long Videos Only
    assert all(r["country_status"] == "match" for r in job["results"])


def test_a_shortfall_is_reported_with_the_reasons(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=False, count=10)
    assert len(job["results"]) == 3
    assert job["stage"] == "found 3 of 10"
    note = " ".join(job["notes"])
    assert "only 3 of 10" in note and "based in a different country" in note


def test_unlisted_country_only_fills_up_after_confirmed_ones(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=True, count=4)
    assert _names(job)[:3] == ["Lahore Kitchen", "Karachi Cooks", "Tiny Kitchen PK"]
    assert job["results"][3]["name"] == "Mystery Masala"
    assert job["results"][3]["country_status"] == "not listed"


def test_minimum_subscribers(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", min_subscribers=1000,
                allow_unlisted_country=False)
    assert "Tiny Kitchen PK" not in _names(job)
    assert job["rejected"]["too_small"] == 1


def test_saved_dismissed_and_already_shown_channels_are_never_suggested(fake_yt):
    dd, _ = fake_yt
    S.add_channels(dd, "@lahorekitchen", 5, rights_confirmed=True)          # by handle
    D.dismiss(dd, ["UCpk2aaaaaaaaaaaaaaaaaaa"])                             # hidden
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=False,
                exclude_ids=["UCsm1aaaaaaaaaaaaaaaaaaa"])                    # shown before
    assert job["results"] == []


def test_without_a_country_filter_about_pages_are_read_only_for_results(fake_yt):
    """Two requests per candidate would double the search time for nothing."""
    dd, calls = fake_yt
    job = _find(dd, language="ur", count=2)
    assert len(job["results"]) == 2
    assert calls["about"] == 2                                  # only the two found


def test_reference_mode_takes_its_language_and_country(fake_yt, monkeypatch):
    dd, calls = fake_yt
    ref_id = "UCpk1aaaaaaaaaaaaaaaaaaa"
    job = _find(dd, mode="like", reference=f"https://www.youtube.com/channel/{ref_id}",
                prompt="", country="same", language="same", count=5,
                allow_unlisted_country=False)
    assert job["effective"]["country"] == "PK"
    assert job["effective"]["language"] == "hi-Latn"
    assert "Lahore Kitchen" not in _names(job)                  # never the reference itself
    assert "Karachi Cooks" in _names(job)
    assert calls["searches"]                                    # searched by its topics


def test_cancel_stops_the_search(fake_yt):
    dd, _ = fake_yt
    job = D.new_job(dd, {"mode": "describe", "prompt": "x y", "count": 5, "use_ai": False})
    D.cancel(dd, job["id"])
    assert D.run(dd, job["id"], Config.load())["status"] == "cancelled"


def test_a_bad_reference_is_refused_with_a_reason(fake_yt):
    dd, _ = fake_yt
    with pytest.raises(ValueError):
        D.new_job(dd, {"mode": "like", "reference": "not a channel", "count": 3})
    with pytest.raises(ShortForgeError):
        D.new_job(dd, {"mode": "describe", "prompt": "  ", "count": 3})


def test_when_the_ai_fails_the_search_carries_on_and_says_so(fake_yt, monkeypatch):
    dd, _ = fake_yt
    monkeypatch.setattr(D, "ai_available", lambda: True)
    monkeypatch.setattr(D, "_ai", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503")))
    job = _find(dd, country="PK", language="ur", use_ai=True, allow_unlisted_country=False)
    assert job["status"] == "done" and len(job["results"]) == 3
    assert any("AI couldn't plan" in n for n in job["notes"])


def test_ai_verdicts_drop_off_topic_channels(fake_yt, monkeypatch):
    dd, _ = fake_yt
    monkeypatch.setattr(D, "ai_available", lambda: True)

    def fake_ai(messages, max_tokens=900):
        text = messages[0]["content"]
        if "search phrases" in text:
            return '{"queries": ["pakistani khana"], "keywords": ["cooking"], "summary": "x"}'
        rows = [ln for ln in text.splitlines() if ln[:1].isdigit()]
        return json.dumps([{"i": int(r.split(".")[0]), "fit": 2 if "Karachi" in r else 8,
                            "language": "ur", "why": "test"} for r in rows])
    monkeypatch.setattr(D, "_ai", fake_ai)
    job = _find(dd, country="PK", language="ur", use_ai=True, allow_unlisted_country=False)
    assert "Karachi Cooks" not in _names(job)
    assert job["rejected"]["not_a_match"] == 1
    assert job["queries"][0] == "pakistani khana"
    assert all(r["fit"] == 8 for r in job["results"])


# --- wishlist ---------------------------------------------------------------------- #

def test_found_channels_go_to_the_wishlist_not_your_channels(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=False)
    added, problems = D.add_to_wishlist(dd, job["results"][:2], 7, rights_confirmed=True,
                                        query_label="test")
    assert len(added) == 2 and not problems
    wish = S.channels_in(dd, S.WISHLIST)
    assert [c["count"] for c in wish] == [7, 7]
    assert wish[0]["country"] == "Pakistan" and wish[0]["channel_id"].startswith("UC")
    assert S.channels_in(dd, S.MAIN) == []


def test_adding_to_the_wishlist_needs_the_rights_confirmation(fake_yt):
    dd, _ = fake_yt
    job = _find(dd, country="PK", language="ur", allow_unlisted_country=False)
    added, problems = D.add_to_wishlist(dd, job["results"], 5, rights_confirmed=False)
    assert added == [] and "rights" in problems[0]


def test_downloads_include_the_wishlist_only_when_asked(tmp_path):
    dd = str(tmp_path)
    S.add_channels(dd, "@mineone", 3, rights_confirmed=True)
    S.add_channels(dd, "@wishone", 3, rights_confirmed=True, list_name=S.WISHLIST)
    assert S.download_keys(dd) == ["@mineone"]                     # off by default
    S.update_settings(dd, include_wishlist=True)
    assert S.download_keys(dd) == ["@mineone", "@wishone"]
    S.update_channel(dd, "@wishone", enabled=False)
    assert S.download_keys(dd) == ["@mineone"]                     # switched off = skipped


def test_moving_a_wishlist_channel_to_your_channels(tmp_path):
    dd = str(tmp_path)
    S.add_channels(dd, "@wishone", 3, rights_confirmed=True, list_name=S.WISHLIST)
    S.update_channel(dd, "@wishone", list=S.MAIN)
    assert S.download_keys(dd, include_wishlist=False) == ["@wishone"]


def test_a_channel_already_saved_is_not_added_twice_under_another_name(tmp_path):
    dd = str(tmp_path)
    S.add_channels(dd, "@samechannel", 3, rights_confirmed=True)
    S.update_channel(dd, "@samechannel", channel_id="UCsameaaaaaaaaaaaaaaaaaa")
    added, problems = S.add_channels(
        dd, "https://www.youtube.com/channel/UCsameaaaaaaaaaaaaaaaaaa", 3,
        rights_confirmed=True, list_name=S.WISHLIST,
        meta={"channel/UCsameaaaaaaaaaaaaaaaaaa": {"channel_id": "UCsameaaaaaaaaaaaaaaaaaa"}})
    assert added == [] and "already saved" in problems[0]


def test_phone_shorts_start_respects_the_wishlist_choice(tmp_path):
    from shortforge.shorts import commands as C
    dd = str(tmp_path)
    S.add_channels(dd, "@mineone", 3, rights_confirmed=True)
    S.add_channels(dd, "@wishone", 4, rights_confirmed=True, list_name=S.WISHLIST)
    assert "from 1 channel(s)" in C.handle("shorts start", dd)
    assert S.load_run(dd)["channels_to_list"] == ["@mineone"]


def test_no_repeat_downloads_cover_wishlist_channels_too(tmp_path):
    """One history for everything: a Short downloaded via a wishlist channel is
    never downloaded again, whichever list its channel is on later."""
    dd = str(tmp_path)
    S.record_download(dd, {"id": "aaaaaaaaaaa", "channel_key": "@wishone"})
    assert "aaaaaaaaaaa" in S.known_ids(dd)


def test_channel_finder_has_its_own_ai_routing_slot():
    from shortforge.providers.store import TASKS
    t = next(t for t in TASKS if t["key"] == "channel_discovery")
    assert t["cats"] == ("llm",)


# --- searching in the wanted language without AI ------------------------------- #

def test_search_words_are_learned_from_channels_in_the_wanted_language():
    """Measured live: English words found 0 of 4 Arabic channels; the same topic
    in Arabic words found them. Words come from channels already found in that
    language — and, for its own script, only words in that script."""
    titles = {"a": ["وصفة سهلة دجاج بالفرن", "طريقة عمل دجاج مشوي", "how to cook chicken"],
              "b": ["دجاج مقلي وصفة سهلة", "طريقة الكبسة"]}
    qs = D.learned_queries(titles, "ar", used=["cooking recipes"])
    assert qs, "nothing learned"
    assert all(not q.isascii() for q in qs)                     # Arabic words only
    words = " ".join(qs).split()
    assert "دجاج" in words and "سهلة" in words                 # shared, frequent words
    assert "chicken" not in words


def test_learned_words_skip_filler_and_what_was_already_searched():
    titles = {"a": ["اكسبلور ترند دجاج", "اكسبلور دجاج", "ولا دجاج سهل"]}
    qs = D.learned_queries(titles, "ar", used=["دجاج"])
    assert not any(w in " ".join(qs) for w in ("اكسبلور", "ترند", "ولا", "دجاج"))


def test_language_hint_is_only_a_valid_youtube_code():
    assert D._hl_for("ar") == "ar"
    assert D._hl_for("he") == "iw"            # YouTube's code for Hebrew
    assert D._hl_for("hi-Latn") == ""         # no such interface language
    assert D._hl_for("") == ""


def test_learning_round_finds_channels_the_first_search_missed(fake_yt, monkeypatch):
    """First search (the operator's English words) finds one Urdu channel; the
    words of its Roman-Urdu titles become the next search, which finds the rest.
    Roman-script Urdu must count — an Urdu target can't insist on Urdu letters."""
    dd, calls = fake_yt
    only_first = {"UCpk1aaaaaaaaaaaaaaaaaaa"}

    def search(q, cfg, limit=30, lang=""):
        calls["searches"].append(q)
        ids = only_first if "pakistani" in q.lower() else set(CATALOGUE)
        return [{"id": c["id"], "url": f"https://www.youtube.com/channel/{c['id']}",
                 "name": c["name"], "handle": c["handle"], "subscribers": c["subs"],
                 "description": "home cooking", "avatar": ""}
                for c in CATALOGUE.values() if c["id"] in ids]
    monkeypatch.setattr(D, "search_channels", search)
    job = _find(dd, prompt="pakistani cooking", language="ur", count=3,
                country="PK", allow_unlisted_country=False)
    assert len(job["results"]) == 3
    assert any("learned from channels" in n for n in job["notes"])
    learned = [q for q in calls["searches"] if "pakistani" not in q.lower()]
    assert learned and any(w in " ".join(learned) for w in ("biryani", "sabzi", "zaroor"))


def test_roman_script_channels_teach_words_for_an_urdu_search():
    """Urdu typed in Latin letters is Urdu; Arabic-script channels still only
    teach their Arabic-script words."""
    titles = {"roman": ["ghar pe biryani kaise banaye", "biryani zaroor try karo"],
              "nastaliq": ["بریانی بنانے کا طریقہ easy", "بریانی ریسپی easy"]}
    qs = D.learned_queries(titles, "ur", used=["pakistani cooking"],
                           detected={"roman": "hi-Latn", "nastaliq": "ur"})
    words = " ".join(qs).split()
    assert "biryani" in words and "بریانی" in words
    assert "easy" not in words


# --- minimum Shorts: any number, counted only when it matters ------------------- #

def _listing_with_counts(counts: dict, seen: list):
    """A fake Shorts tab whose length is the channel's real Shorts count."""
    def listing(url, cfg, limit, lang=""):
        cid = url.split("/channel/")[1].split("/")[0]
        c = CATALOGUE[cid]
        n = counts.get(cid, 6)
        seen.append((cid, limit))
        return {"channel": c["name"], "uploader_id": c["handle"], "channel_id": c["id"],
                "channel_follower_count": c["subs"],
                "entries": [{"id": f"v{i}", "title": c["titles"][i % len(c["titles"])],
                             "view_count": 1000} for i in range(min(n, limit))]}
    return listing


def test_minimum_shorts_above_15_is_counted_and_enforced(fake_yt, monkeypatch):
    """The operator asked for no 1–15 limit. Above 15 a channel's Shorts are
    counted — paging through its Shorts tab — but only for channels that passed
    every other check, and never further than the minimum."""
    dd, _ = fake_yt
    seen = []
    monkeypatch.setattr(D, "_listing", _listing_with_counts(
        {"UCpk1aaaaaaaaaaaaaaaaaaa": 500, "UCpk2aaaaaaaaaaaaaaaaaaa": 20}, seen))
    job = _find(dd, language="ur", country="PK", count=5, min_shorts=40,
                allow_unlisted_country=False)
    assert _names(job) == ["Lahore Kitchen"]
    assert job["results"][0]["shorts_seen"] == 40                  # "at least 40"
    # Karachi Cooks (20) is counted and turned down; the rest have under 15
    # Shorts in all, which the first look already shows — no counting needed
    assert job["rejected"].get("too_few_shorts", 0) >= 1
    deep = [(cid, lim) for cid, lim in seen if lim > D.SAMPLE]
    assert sorted(cid for cid, _ in deep) == ["UCpk1aaaaaaaaaaaaaaaaaaa",
                                              "UCpk2aaaaaaaaaaaaaaaaaaa"]
    assert all(lim == 40 for _, lim in deep)


def test_minimum_shorts_up_to_15_needs_no_extra_counting(fake_yt, monkeypatch):
    dd, _ = fake_yt
    seen = []
    monkeypatch.setattr(D, "_listing", _listing_with_counts(
        {"UCpk1aaaaaaaaaaaaaaaaaaa": 15, "UCpk2aaaaaaaaaaaaaaaaaaa": 4}, seen))
    job = _find(dd, language="ur", country="PK", count=5, min_shorts=10,
                allow_unlisted_country=False)
    assert "Lahore Kitchen" in _names(job) and "Karachi Cooks" not in _names(job)
    assert all(lim == D.SAMPLE for _, lim in seen)


def test_any_minimum_is_accepted_when_the_search_is_created(tmp_path):
    job = D.new_job(str(tmp_path), {"mode": "describe", "prompt": "cooking", "min_shorts": 750})
    assert job["params"]["min_shorts"] == 750
