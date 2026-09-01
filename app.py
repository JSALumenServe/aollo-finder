import os
import csv
import io
import time
import requests
import openpyxl
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

app = Flask(__name__)

APOLLO_API_KEY = os.getenv("APOLLO_API_KEY")
SUPABASE_URL   = os.getenv("SUPABASE_URL")
SUPABASE_KEY   = os.getenv("SUPABASE_KEY")

APOLLO_SEARCH_URL = "https://api.apollo.io/v1/mixed_people/api_search"
APOLLO_ENRICH_URL = "https://api.apollo.io/v1/people/match"
APP_BASE_URL      = "https://apollo-finder-ls.onrender.com"

db = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        db = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        app.logger.error(f"Supabase init failed: {e}")

_cached_credits = {"remaining": None, "used": None}


def apollo_headers():
    return {"X-Api-Key": APOLLO_API_KEY, "Content-Type": "application/json"}


def update_credits_cache(result: dict):
    """Pull credit info from any Apollo enrichment response and cache it."""
    remaining = result.get("credits_remaining")
    used = result.get("credits_used")
    if remaining is not None:
        _cached_credits["remaining"] = remaining
    if used is not None:
        _cached_credits["used"] = used


def log_usage(action: str, data: dict):
    if db is None:
        return
    try:
        db.table("usage_log").insert({
            "action":          action,
            "search_name":     data.get("search_name"),
            "search_company":  data.get("search_company"),
            "search_title":    data.get("search_title"),
            "search_location": data.get("search_location"),
            "search_industry": data.get("search_industry"),
            "results_count":   data.get("results_count"),
            "reveal_type":     data.get("reveal_type"),
            "contact_name":    data.get("contact_name"),
            "contact_company": data.get("contact_company"),
        }).execute()
    except Exception as e:
        app.logger.error(f"Supabase log error: {e}")


def search_apollo(params: dict) -> dict:
    payload = {"page": 1, "per_page": params.get("per_page", 10)}
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
    org          = person.get("organization") or {}
    email        = person.get("email", "")
    phone        = person.get("sanitized_phone", "")
    has_email    = True  # Apollo search doesn't reliably surface availability; always allow reveal
    has_phone    = True
    return {
        "id":             person.get("id", ""),
        "name":           f"{person.get('first_name', '')} {person.get('last_name', '')}".strip(),
        "title":          person.get("title", "—"),
        "company":        person.get("organization_name") or org.get("name", "—"),
        "email":          email,
        "email_revealed": bool(email and "@" in email),
        "has_email":      has_email,
        "phone":          phone,
        "phone_revealed": bool(phone),
        "has_phone":      has_phone,
        "linkedin":       person.get("linkedin_url", ""),
        "location":       ", ".join(filter(None, [person.get("city", ""), person.get("state", "")])),
    }


def check_availability(person_id: str) -> dict:
    """Check Apollo for a contact — captures actual email/phone if already revealed."""
    try:
        resp = requests.post(
            APOLLO_ENRICH_URL,
            json={"id": person_id},
            headers=apollo_headers(),
            timeout=8,
        )
        if not resp.ok:
            return {"has_email": None, "has_phone": None, "email": None, "phone": None}
        p      = resp.json().get("person") or {}
        email  = p.get("email", "") or ""
        status = p.get("email_status", "")
        phones = p.get("phone_numbers") or []
        phone  = p.get("sanitized_phone", "") or ""
        if not phone and phones:
            phone = phones[0].get("sanitized_number") or phones[0].get("raw_number") or ""
        clean_email = email if "@" in email else None
        has_email   = bool(clean_email or status in ("verified", "unverified", "likely to engage"))
        has_phone   = bool(phones or phone)
        # Save to DB if Apollo returned real values (previously revealed data comes back free)
        if db is not None and (clean_email or phone):
            try:
                db.table("revealed_contacts").upsert({
                    "person_id": person_id,
                    **({"email": clean_email} if clean_email else {}),
                    **({"phone": phone} if phone else {}),
                }).execute()
            except Exception:
                pass
        return {"has_email": has_email, "has_phone": has_phone, "email": clean_email, "phone": phone}
    except Exception:
        return {"has_email": None, "has_phone": None, "email": None, "phone": None}


def parse_company_list(file) -> list:
    """Parse uploaded CSV or Excel and return list of company names."""
    filename = file.filename.lower()
    companies = []
    if filename.endswith(".csv"):
        content = file.read().decode("utf-8", errors="ignore")
        reader  = csv.reader(io.StringIO(content))
        for row in reader:
            if row and row[0].strip() and row[0].strip().lower() != "company":
                companies.append(row[0].strip())
    elif filename.endswith((".xlsx", ".xls")):
        wb  = openpyxl.load_workbook(file, read_only=True, data_only=True)
        ws  = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if not row or not row[0]:
                continue
            val = str(row[0]).strip()
            if i == 0 and val.lower() in ("company", "company name", "name"):
                continue
            if val:
                companies.append(val)
    return companies


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/bulk")
def bulk():
    return render_template("bulk.html")


@app.route("/stats")
def stats():
    try:
        rows = db.table("usage_log").select("*").order("created_at", desc=True).limit(200).execute()
        return render_template("stats.html", logs=rows.data)
    except Exception as e:
        return f"Error loading stats: {e}", 500


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

    result   = search_apollo(search_params)
    if "error" in result:
        return jsonify(result), 500

    people   = result.get("people", [])
    contacts = [format_contact(p) for p in people]
    credits  = {"remaining": result.get("credits_remaining")}

    # Restore any previously revealed email/phone from database
    if contacts and db is not None:
        try:
            ids  = [c["id"] for c in contacts]
            rows = db.table("revealed_contacts").select("person_id,email,phone").in_("person_id", ids).execute()
            saved = {r["person_id"]: r for r in (rows.data or [])}
            for c in contacts:
                r = saved.get(c["id"])
                if r:
                    if r.get("email") and "@" in r["email"]:
                        c["email"] = r["email"]
                        c["email_revealed"] = True
                    if r.get("phone"):
                        c["phone"] = r["phone"]
                        c["phone_revealed"] = True
        except Exception as e:
            app.logger.error(f"Restore reveals error: {e}")

    # Check availability — also captures real email/phone if Apollo returns them for free
    if contacts:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(check_availability, c["id"]): i for i, c in enumerate(contacts)}
            for future in as_completed(futures):
                i  = futures[future]
                av = future.result()
                contacts[i]["has_email"] = av["has_email"]
                contacts[i]["has_phone"] = av["has_phone"]
                if av.get("email") and not contacts[i]["email_revealed"]:
                    contacts[i]["email"] = av["email"]
                    contacts[i]["email_revealed"] = True
                if av.get("phone") and not contacts[i]["phone_revealed"]:
                    contacts[i]["phone"] = av["phone"]
                    contacts[i]["phone_revealed"] = True

    log_usage("search", {
        "search_name":     search_params["name"],
        "search_company":  search_params["company"],
        "search_title":    search_params["title"],
        "search_location": search_params["location"],
        "search_industry": search_params["industry"],
        "results_count":   len(contacts),
    })
    return jsonify({"contacts": contacts, "count": len(contacts), "credits": credits})


@app.route("/bulk-search", methods=["POST"])
def bulk_search():
    file     = request.files.get("file")
    titles   = request.form.get("titles", "").strip()
    location = request.form.get("location", "").strip()

    if not file or not file.filename:
        return jsonify({"error": "Please upload a file."}), 400
    if not titles:
        return jsonify({"error": "Please enter at least one job title."}), 400

    companies = parse_company_list(file)
    if not companies:
        return jsonify({"error": "No company names found in the file."}), 400

    def search_one(company):
        result = search_apollo({"company": company, "title": titles, "location": location})
        contacts = []
        if "error" not in result:
            for p in result.get("people", []):
                c = format_contact(p)
                c["searched_company"] = company
                contacts.append(c)
        return contacts

    all_contacts = []
    batch_size   = 8   # parallel workers — stays well under Apollo rate limits
    company_list = companies[:150]
    for i in range(0, len(company_list), batch_size):
        batch = company_list[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=batch_size) as ex:
            for result in ex.map(search_one, batch):
                all_contacts.extend(result)

    # Restore previously revealed data (single DB call, no per-contact API calls)
    if all_contacts and db is not None:
        try:
            ids   = [c["id"] for c in all_contacts]
            # Supabase IN filter supports up to 1000 values
            rows  = db.table("revealed_contacts").select("person_id,email,phone").in_("person_id", ids).execute()
            saved = {r["person_id"]: r for r in (rows.data or [])}
            for c in all_contacts:
                r = saved.get(c["id"])
                if r:
                    if r.get("email") and "@" in r["email"]:
                        c["email"] = r["email"]
                        c["email_revealed"] = True
                    if r.get("phone"):
                        c["phone"] = r["phone"]
                        c["phone_revealed"] = True
        except Exception as e:
            app.logger.error(f"Restore reveals error: {e}")

    total_in_file = len(companies)
    searched      = len(company_list)
    skipped       = total_in_file - searched

    log_usage("bulk_search", {
        "search_company":  f"{searched} of {total_in_file} companies",
        "search_title":    titles,
        "search_location": location,
        "results_count":   len(all_contacts),
    })

    return jsonify({
        "contacts":   all_contacts,
        "count":      len(all_contacts),
        "companies":  searched,
        "skipped":    skipped,
        "total":      total_in_file,
    })


@app.route("/credits")
def credits():
    """Return cached Apollo credit balance (updated after every reveal). No API call needed."""
    return jsonify(_cached_credits.copy())


@app.route("/reveal", methods=["POST"])
def reveal():
    data            = request.get_json()
    person_id       = data.get("id", "").strip()
    reveal_type     = data.get("type", "email")
    contact_name    = data.get("contact_name", "")
    contact_company = data.get("contact_company", "")

    if not person_id:
        return jsonify({"error": "No contact ID provided."}), 400

    if reveal_type == "phone":
        payload = {"id": person_id, "reveal_phone_number": True, "webhook_url": f"{APP_BASE_URL}/webhook/phone"}
        try:
            resp = requests.post(APOLLO_ENRICH_URL, json=payload, headers=apollo_headers(), timeout=15)
            resp.raise_for_status()
            result = resp.json()
            person = result.get("person") or {}
            update_credits_cache(result)
            log_usage("reveal", {"reveal_type": "phone", "contact_name": contact_name, "contact_company": contact_company})

            # Check if Apollo returned the phone synchronously (already-revealed contacts)
            phone = ""
            for pn in (person.get("phone_numbers") or []):
                phone = pn.get("sanitized_number") or pn.get("raw_number") or ""
                if phone:
                    break
            phone = phone or person.get("sanitized_phone") or person.get("mobile_phone") or ""
            if phone and db is not None:
                try:
                    db.table("phone_results").upsert({"person_id": person_id, "phone": phone}).execute()
                    db.table("revealed_contacts").upsert({"person_id": person_id, "phone": phone}).execute()
                except Exception:
                    pass
            if phone:
                return jsonify({"phone": phone, "person_id": person_id})

            # Phone not in response — async path; webhook will deliver it
            enrichment = result.get("phone_enrichment") or {}
            apollo_request_id = enrichment.get("request_id") or str(result.get("request_id") or "")
            if apollo_request_id and db is not None:
                try:
                    db.table("phone_results").upsert({"person_id": person_id, "apollo_request_id": apollo_request_id}).execute()
                except Exception:
                    pass
            return jsonify({"queued": True, "person_id": person_id, "apollo_request_id": apollo_request_id})
        except requests.exceptions.HTTPError as e:
            return jsonify({"error": f"Apollo API error: {e.response.status_code} — {e.response.text}"}), 500
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # Email — synchronous
    payload = {"id": person_id, "reveal_personal_emails": True}
    try:
        resp = requests.post(APOLLO_ENRICH_URL, json=payload, headers=apollo_headers(), timeout=15)
        resp.raise_for_status()
        result  = resp.json()
        person  = result.get("person", {})
        email   = person.get("email", "")
        update_credits_cache(result)
        credits = _cached_credits.copy()
        clean_email = email if "@" in (email or "") else None
        if clean_email and db is not None:
            try:
                db.table("revealed_contacts").upsert({"person_id": person_id, "email": clean_email}).execute()
            except Exception as e:
                app.logger.error(f"Save reveal error: {e}")
        log_usage("reveal", {"reveal_type": "email", "contact_name": contact_name, "contact_company": contact_company})
        return jsonify({"email": clean_email, "credits": credits})
    except requests.exceptions.HTTPError as e:
        return jsonify({"error": f"Apollo API error: {e.response.status_code} — {e.response.text}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500




@app.route("/webhook/phone", methods=["POST"])
def webhook_phone():
    """Apollo posts phone results here asynchronously."""
    payload = request.get_json(silent=True) or {}
    app.logger.info(f"WEBHOOK RECEIVED: keys={list(payload.keys())}")
    # Log raw payload to DB for diagnostics
    if db is not None:
        try:
            import json as _json
            db.table("phone_results").upsert({"person_id": f"__webhook_log_{payload.get('request_id','?')}", "phone": _json.dumps(payload)[:500]}).execute()
        except Exception:
            pass
    people  = []
    if payload.get("person"):   people.append(payload["person"])
    if payload.get("people"):   people.extend(payload["people"])
    if payload.get("contacts"): people.extend(payload["contacts"])
    if not people and payload.get("id"):
        people.append(payload)

    for p in people:
        pid   = p.get("id") or p.get("person_id") or ""
        phone = ""
        for pn in (p.get("phone_numbers") or []):
            phone = pn.get("sanitized_number") or pn.get("raw_number") or ""
            if phone:
                break
        phone = phone or p.get("sanitized_phone") or p.get("mobile_phone") or ""
        if pid and phone and db is not None:
            try:
                db.table("phone_results").upsert({"person_id": pid, "phone": phone}).execute()
                db.table("revealed_contacts").upsert({"person_id": pid, "phone": phone}).execute()
            except Exception as e:
                app.logger.error(f"Phone webhook store error: {e}")

    return jsonify({"ok": True}), 200


@app.route("/phone-result/<person_id>", methods=["GET"])
def phone_result(person_id):
    """Check DB for webhook-delivered result. Pass ?final=1 for one direct Apollo check at end of polling."""
    if db is not None:
        try:
            rows = db.table("phone_results").select("phone").eq("person_id", person_id).execute()
            row = rows.data[0] if rows.data else None
            if row and row.get("phone") and not str(row["phone"]).startswith("__webhook_log"):
                return jsonify({"phone": row["phone"]})
        except Exception:
            pass

    # Only call Apollo directly on the final poll (caller passes ?final=1)
    if request.args.get("final") == "1":
        try:
            resp = requests.post(APOLLO_ENRICH_URL, json={"id": person_id, "reveal_phone_number": True,
                                 "webhook_url": f"{APP_BASE_URL}/webhook/phone"},
                                 headers=apollo_headers(), timeout=10)
            if resp.ok:
                person = resp.json().get("person") or {}
                phone = ""
                for pn in (person.get("phone_numbers") or []):
                    phone = pn.get("sanitized_number") or pn.get("raw_number") or ""
                    if phone:
                        break
                phone = phone or person.get("sanitized_phone") or person.get("mobile_phone") or ""
                if phone:
                    if db is not None:
                        try:
                            db.table("phone_results").upsert({"person_id": person_id, "phone": phone}).execute()
                            db.table("revealed_contacts").upsert({"person_id": person_id, "phone": phone}).execute()
                        except Exception:
                            pass
                    return jsonify({"phone": phone})
        except Exception:
            pass

    return jsonify({"phone": None})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
