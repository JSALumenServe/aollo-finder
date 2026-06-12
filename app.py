import os
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY") or "qCRth4TImuo1MbHQeQCd1A"

APOLLO_PEOPLE_URL = "https://api.apollo.io/v1/mixed_people/api_search"


def search_apollo(params: dict) -> dict:
    payload = {
        "page": 1,
        "per_page": 10,
    }

    if params.get("name"):
        payload["q_keywords"] = params["name"]
    if params.get("company"):
        payload["q_organization_name"] = params["company"]
    if params.get("title"):
        payload["person_titles"] = [t.strip() for t in params["title"].split(",") if t.strip()]
    if params.get("location"):
        payload["person_locations"] = [params["location"]]
    if params.get("industry"):
        payload["organization_industry_tag_ids"] = []
        payload["q_organization_keyword_tags"] = [params["industry"]]

    headers = {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json"}

    print(f"DEBUG key={APOLLO_API_KEY!r} url={APOLLO_PEOPLE_URL} payload={payload}")

    try:
        resp = requests.post(APOLLO_PEOPLE_URL, json=payload, headers=headers, timeout=15)
        print(f"DEBUG status={resp.status_code} body={resp.text[:300]}")
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        return {"error": f"Apollo API error: {e.response.status_code} — {e.response.text}"}
    except Exception as e:
        return {"error": str(e)}


def format_contact(person: dict) -> dict:
    org = person.get("organization") or {}
    return {
        "name": f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
        "title": person.get("title", "—"),
        "company": person.get("organization_name") or org.get("name", "—"),
        "email": person.get("email") or "Not revealed (free plan limit)",
        "linkedin": person.get("linkedin_url", ""),
        "location": person.get("city", "") + (", " + person.get("state", "") if person.get("state") else ""),
        "phone": person.get("sanitized_phone", ""),
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

    people = result.get("people", [])
    contacts = [format_contact(p) for p in people]

    credits = {
        "used": result.get("num_fetch_result"),
        "remaining": result.get("credits_remaining"),
        "total": result.get("credits_used"),
    }

    return jsonify({"contacts": contacts, "count": len(contacts), "credits": credits})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
