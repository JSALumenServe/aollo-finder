import os
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)

APOLLO_API_KEY  = os.getenv("APOLLO_API_KEY") or "qCRth4TImuo1MbHQeQCd1A"
SUPABASE_URL    = os.getenv("SUPABASE_URL") or "https://viahldmykvnbdfmbwqjy.supabase.co"
SUPABASE_KEY    = os.getenv("SUPABASE_KEY") or "sb_publishable_oAZHo8jh8UNqV-s0OBLnQQ__jLJxwf-"

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/api_search"
APOLLO_ENRICH_URL = "https://api.apollo.io/v1/people/match"

db = create_client(SUPABASE_URL, SUPABASE_KEY)


def apollo_headers():
    return {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json"}


def log_usage(action: str, data: dict):
    try:
        db.table("usage_log").insert({
            "action":           action,
            "search_name":      data.get("search_name"),
            "search_company":   data.get("search_company"),
            "search_title":     data.get("search_title"),
            "search_location":  data.get("search_location"),
            "search_industry":  data.get("search_industry"),
            "results_count":    data.get("results_count"),
            "reveal_type":      data.get("reveal_type"),
            "contact_name":     data.get("contact_name"),
            "contact_company":  data.get("contact_company"),
        }).execute()
    except Exception as e:
        app.logger.error(f"Supabase log error: {e}")


def search_apollo(params: dict) -> dict:
    payload = {"page": 1, "per_page": 10}

    if params.get("name"):
        payload["q_keywords"] = params["name"]
    if params.get("company"):
        payload["q_organization_name"] = params["company"]
    if params.get("title"):
        payload["person_titles"] = [t.strip() for t in params["title"].split(",") if t.strip()]
    if params.get("location"):
        payload["person_locations"] = [params["location"]]
    if params.get("industry"):
        payload["q_organization_keyword_tags"] = [params["industry"]]

    try:
        resp = requests.post(APOLLO_SEARCH_URL, json=payload, headers=apollo_headers(), timeout=15)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        return {"error": f"Apollo API error: {e.response.status_code} — {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


def format_contact(person: dict) -> dict:
    org = person.get("organization") or {}
    email = person.get("email", "")
    phone = person.get("sanitized_phone", "")
    return {
        "id":             person.get("id", ""),
        "name":           f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
        "title":          person.get("title", "—"),
        "company":        person.get("organization_name") or org.get("name", "—"),
        "email":          email,
        "email_revealed": bool(email and "@" in email),
        "phone":          phone,
        "phone_revealed": bool(phone),
        "linkedin":       person.get("linkedin_url", ""),
        "location":       ", ".join(filter(None, [person.get("city", ""), person.get("state", "")])),
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/search", methods=["POST"])
def search():
    data = request.get_json()
    search_params = {
        "name":     data.get("name", "").strip(),
        "company":  data.get("company", "").strip(),
        "title":    data.get("title", "").strip(),
        "location": data.get("location", "").strip(),
        "industry": data.get("industry", "").strip(),
    }
    if not any(search_params.values()):
        return jsonify({"error": "Please fill in at least one search field."}), 400

    result = search_apollo(search_params)

    if "error" in result:
        return jsonify(result), 500

    people  = result.get("people", [])
    contacts = [format_contact(p) for p in people]
    credits  = {"remaining": result.get("credits_remaining")}

    log_usage("search", {
        "search_name":     search_params["name"],
        "search_company":  search_params["company"],
        "search_title":    search_params["title"],
        "search_location": search_params["location"],
        "search_industry": search_params["industry"],
        "results_count":   len(contacts),
    })

    return jsonify({"contacts": contacts, "count": len(contacts), "credits": credits})


@app.route("/reveal", methods=["POST"])
def reveal():
    data = request.get_json()
    person_id   = data.get("id", "").strip()
    reveal_type = data.get("type", "email")
    contact_name    = data.get("contact_name", "")
    contact_company = data.get("contact_company", "")

    if not person_id:
        return jsonify({"error": "No contact ID provided."}), 400

    payload = {
        "id": person_id,
        "reveal_personal_emails": reveal_type == "email",
        "reveal_phone_number":    reveal_type == "phone",
    }

    try:
        resp = requests.post(APOLLO_ENRICH_URL, json=payload, headers=apollo_headers(), timeout=15)
        resp.raise_for_status()
        result = resp.json()
        person = result.get("person", {})

        email   = person.get("email", "")
        phone   = person.get("sanitized_phone", "")
        credits = {"remaining": result.get("credits_remaining")}

        log_usage("reveal", {
            "reveal_type":     reveal_type,
            "contact_name":    contact_name,
            "contact_company": contact_company,
        })

        return jsonify({
            "email":   email if "@" in (email or "") else None,
            "phone":   phone or None,
            "credits": credits,
        })
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"Apollo API error: {e.response.status_code} — {e.response.text}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/stats")
def stats():
    try:
        rows = db.table("usage_log").select("*").order("created_at", desc=True).limit(200).execute()
        return render_template("stats.html", logs=rows.data)
    except Exception as e:
        return f"Error loading stats: {e}", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
