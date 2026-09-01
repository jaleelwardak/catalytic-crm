"""
Catalytic Converter Buyer CRM + Google Places Lead Finder

Run:
    pip install streamlit requests pandas pdfplumber
    streamlit run catalytic_converter_crm.py

Set your Google Places API key:
    macOS/Linux:
        export GOOGLE_PLACES_API_KEY="YOUR_KEY"
    Windows PowerShell:
        $env:GOOGLE_PLACES_API_KEY="YOUR_KEY"

The app stores CRM data locally in catalytic_converter_crm.csv.
"""

import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import pdfplumber
import requests
import streamlit as st


API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "YOUR_GOOGLE_PLACES_API_KEY")
DATA_FILE = Path("catalytic_converter_crm.csv")
PURCHASE_FILE = Path("catalytic_converter_purchases.csv")
PRICE_FILE = Path("catalytic_converter_price_sheet.csv")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_BASE = "https://places.googleapis.com/v1/places"

LEAD_COLUMNS = [
    "place_id", "shop_name", "phone", "address", "city", "website",
    "maps_url", "latitude", "longitude", "rating", "review_count",
    "business_status", "source_query",
    "status", "cats_available", "last_called", "next_follow_up",
    "last_contact_note", "last_purchase_date", "purchase_count",
    "total_cats_bought", "total_spent", "route_flag", "priority",
    "created_at", "updated_at",
]

PURCHASE_COLUMNS = [
    "purchase_id", "date", "shop_name", "place_id", "cat_count",
    "amount_paid", "projected_revenue", "projected_profit",
    "multiple", "notes",
]

PRICE_COLUMNS = [
    "cat_code", "price_display", "price_numeric", "unit", "effective_date",
]

PRICE_ROW_RE = re.compile(r'^\$[\d,]+\.\d{2}/(EA|LB)$')


def api_headers(field_mask):
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": field_mask,
    }


def require_key():
    return API_KEY and API_KEY != "YOUR_GOOGLE_PLACES_API_KEY"


def load_csv(path, columns):
    if path.exists():
        df = pd.read_csv(path, dtype=str).fillna("")
        for c in columns:
            if c not in df.columns:
                df[c] = ""
        return df[columns]
    return pd.DataFrame(columns=columns)


def save_df(df, path):
    df.to_csv(path, index=False)


def search_places(query, city, max_results=20):
    """Google Places API (New) Text Search."""
    if not require_key():
        raise RuntimeError("Set GOOGLE_PLACES_API_KEY before searching.")

    payload = {
        "textQuery": f"{query} in {city}",
        "pageSize": min(max_results, 20),
        "includedType": "car_repair",
        "strictTypeFiltering": False,
    }

    field_mask = ",".join([
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.googleMapsUri",
        "places.location",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
    ])

    r = requests.post(
        SEARCH_URL,
        json=payload,
        headers=api_headers(field_mask),
        timeout=25,
    )
    r.raise_for_status()
    return r.json().get("places", [])


def details(place_id):
    """Fetch contact details only when needed."""
    if not require_key():
        return {}

    field_mask = ",".join([
        "id", "displayName", "formattedAddress",
        "nationalPhoneNumber", "websiteUri", "googleMapsUri",
        "location", "rating", "userRatingCount", "businessStatus",
    ])

    r = requests.get(
        f"{DETAILS_BASE}/{place_id}",
        headers=api_headers(field_mask),
        timeout=25,
    )
    r.raise_for_status()
    return r.json()


def normalize_place(p, query):
    location = p.get("location", {})
    display = p.get("displayName", {}) or {}
    address = p.get("formattedAddress", "")
    city = ""
    parts = [x.strip() for x in address.split(",") if x.strip()]
    if len(parts) >= 3:
        city = parts[-3]
    elif parts:
        city = parts[-1]

    return {
        "place_id": p.get("id", ""),
        "shop_name": display.get("text", ""),
        "phone": p.get("nationalPhoneNumber", ""),
        "address": address,
        "city": city,
        "website": p.get("websiteUri", ""),
        "maps_url": p.get("googleMapsUri", ""),
        "latitude": location.get("latitude", ""),
        "longitude": location.get("longitude", ""),
        "rating": p.get("rating", ""),
        "review_count": p.get("userRatingCount", ""),
        "business_status": p.get("businessStatus", ""),
        "source_query": query,
    }


def upsert_leads(new_places, leads):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing = {str(x): i for i, x in enumerate(leads["place_id"].tolist())}

    for p in new_places:
        pid = p["place_id"]
        if not pid:
            continue

        row = normalize_place(p, p.get("_query", ""))
        if pid in existing:
            i = existing[pid]
            # Refresh public business data but preserve CRM history.
            for key in [
                "shop_name", "phone", "address", "city", "website",
                "maps_url", "latitude", "longitude", "rating",
                "review_count", "business_status", "source_query",
            ]:
                if row.get(key):
                    leads.loc[i, key] = row[key]
            leads.loc[i, "updated_at"] = now
        else:
            base = {c: "" for c in LEAD_COLUMNS}
            base.update(row)
            base.update({
                "status": "Not Called",
                "cats_available": "Unknown",
                "purchase_count": "0",
                "total_cats_bought": "0",
                "total_spent": "0",
                "route_flag": "No",
                "priority": "Normal",
                "created_at": now,
                "updated_at": now,
            })
            leads = pd.concat([leads, pd.DataFrame([base])], ignore_index=True)

    return leads


def apple_maps_link(address):
    return f"https://maps.apple.com/?address={quote_plus(address)}"


def google_maps_directions(address):
    return f"https://www.google.com/maps/dir/?api=1&destination={quote_plus(address)}"


def projected_profit(amount_paid, multiple):
    revenue = amount_paid * multiple
    return revenue - amount_paid


def _col_of(x0):
    """Bucket a word's x-position into one of the sheet's 3 price columns."""
    if x0 < 210:
        return 0
    elif x0 < 380:
        return 1
    else:
        return 2


def _parse_price_page(page):
    """Pull cat_code/price pairs off one page, ignoring watermark/header text.

    The refinery sheet stamps a repeating watermark string across every
    page at 18pt and any header/footer boilerplate at 11-15pt. Actual
    price-list rows are consistently 8pt, so filtering on font size is a
    reliable way to isolate real data before reconstructing rows/columns
    from word positions.
    """
    words = page.extract_words(extra_attrs=["size"])
    real = [w for w in words if round(w["size"], 1) == 8.0]

    cols = {0: [], 1: [], 2: []}
    for w in real:
        cols[_col_of(w["x0"])].append(w)

    records = []
    for _, wds in cols.items():
        lines = {}
        for w in wds:
            key = round(w["top"])
            lines.setdefault(key, []).append(w)

        sorted_tops = sorted(lines.keys())
        merged = []
        for t in sorted_tops:
            if merged and abs(t - merged[-1][-1]) <= 2:
                merged[-1].append(t)
            else:
                merged.append([t])

        line_list = []
        for group in merged:
            ws = []
            for t in group:
                ws.extend(lines[t])
            ws.sort(key=lambda w: w["x0"])
            line_list.append((sum(group) / len(group), ws))
        line_list.sort(key=lambda x: x[0])

        cur_name_parts, cur_price, prev_top = [], None, None

        def flush():
            if cur_name_parts and cur_price:
                records.append((" ".join(cur_name_parts).strip(), cur_price))

        for top, ws in line_list:
            texts = [w["text"] for w in ws]
            name_tokens, price_tokens = [], []
            i = 0
            while i < len(texts):
                t = texts[i]
                if PRICE_ROW_RE.match(t):
                    price_tokens.append(t)
                    i += 1
                elif t == "NO" and i + 1 < len(texts) and texts[i + 1] == "VALUE":
                    price_tokens.append("NO VALUE")
                    i += 2
                else:
                    name_tokens.append(t)
                    i += 1

            # A wrapped continuation of the previous row's code sits much
            # closer vertically (~8pt) than the normal row spacing (~19pt).
            is_continuation = prev_top is not None and (top - prev_top) < 12
            if is_continuation:
                cur_name_parts.append(" ".join(name_tokens))
                if price_tokens:
                    cur_price = price_tokens[0]
            else:
                flush()
                cur_name_parts = name_tokens
                cur_price = price_tokens[0] if price_tokens else None

            prev_top = top

        flush()

    return records


def parse_price_sheet_pdf(file_obj):
    """Parse the weekly refinery price sheet PDF into a DataFrame.

    Returns a dataframe with PRICE_COLUMNS, or raises ValueError if no
    rows could be extracted (e.g. the sheet's layout template changed).
    """
    all_records = []
    with pdfplumber.open(file_obj) as pdf:
        for page in pdf.pages:
            all_records.extend(_parse_price_page(page))

    if not all_records:
        raise ValueError(
            "No price rows found. The sheet's layout may have changed — "
            "double-check a page or two by hand before relying on this."
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for code, price in all_records:
        numeric = ""
        unit = ""
        m = re.match(r'^\$([\d,]+\.\d{2})/(EA|LB)$', price)
        if m:
            numeric = m.group(1).replace(",", "")
            unit = m.group(2)
        rows.append({
            "cat_code": code,
            "price_display": price,
            "price_numeric": numeric,
            "unit": unit,
            "effective_date": now,
        })

    df = pd.DataFrame(rows, columns=PRICE_COLUMNS)
    # Refinery sheets occasionally repeat a code across columns/pages;
    # keep the last occurrence rather than silently averaging or dropping.
    df = df.drop_duplicates(subset="cat_code", keep="last").reset_index(drop=True)
    return df


st.set_page_config(
    page_title="Cat Buyer CRM",
    page_icon="🚗",
    layout="wide",
)

st.title("🚗 Catalytic Converter Buyer CRM")
st.caption("Lead finder + call tracker + purchase history + daily cash/profit tracking")

leads = load_csv(DATA_FILE, LEAD_COLUMNS)
purchases = load_csv(PURCHASE_FILE, PURCHASE_COLUMNS)
prices = load_csv(PRICE_FILE, PRICE_COLUMNS)

tabs = st.tabs([
    "🔎 Find Shops",
    "📞 Call Queue",
    "🗺️ Route",
    "💵 Price Sheet",
    "💰 Today's Run",
    "🏪 CRM",
    "📈 Stats",
])

# ----------------------------------------------------------------------
# FIND SHOPS
# ----------------------------------------------------------------------
with tabs[0]:
    st.subheader("Find every shop you can reasonably reach")
    st.info(
        "Use several search phrases for the same city. This reduces dependence "
        "on one Google query and makes your call list repeatable."
    )

    c1, c2, c3 = st.columns([2, 2, 1])
    city = c1.text_input("City / target area", placeholder="Tacoma, WA")
    max_results = c2.slider("Results per search", 5, 20, 20)
    run_search = c3.button("Search", type="primary", use_container_width=True)

    queries = st.multiselect(
        "Search categories",
        [
            "auto repair shop",
            "muffler shop",
            "exhaust shop",
            "automotive repair",
            "mechanic",
            "transmission shop",
            "auto body shop",
        ],
        default=[
            "auto repair shop",
            "muffler shop",
            "exhaust shop",
            "automotive repair",
        ],
    )

    if run_search:
        if not city.strip():
            st.error("Enter a city or target area.")
        elif not require_key():
            st.error("Set GOOGLE_PLACES_API_KEY before searching.")
        else:
            found = []
            progress = st.progress(0)
            for i, q in enumerate(queries):
                try:
                    results = search_places(q, city.strip(), max_results)
                    for p in results:
                        p["_query"] = q
                        found.append(p)
                except Exception as exc:
                    st.warning(f"{q}: {exc}")
                progress.progress((i + 1) / max(len(queries), 1))
                time.sleep(0.1)

            # Deduplicate by Place ID.
            unique = {}
            for p in found:
                if p.get("id"):
                    unique[p["id"]] = p

            leads = upsert_leads(list(unique.values()), leads)
            save_df(leads, DATA_FILE)
            st.success(f"Added/refreshed {len(unique)} unique shops. Total CRM leads: {len(leads)}")

    st.write(f"**CRM total:** {len(leads)} shops")

# ----------------------------------------------------------------------
# CALL QUEUE
# ----------------------------------------------------------------------
with tabs[1]:
    st.subheader("Today's calling queue")

    if leads.empty:
        st.write("Search for shops first.")
    else:
        statuses = [
            "Not Called",
            "Called - No Cats",
            "Cats Likely",
            "Cats Confirmed",
            "Purchased Before",
            "Do Not Call",
        ]

        f1, f2, f3 = st.columns(3)
        selected_status = f1.multiselect(
            "Status",
            statuses,
            default=["Not Called", "Cats Likely", "Cats Confirmed", "Purchased Before"],
        )
        city_filter = f2.text_input("Filter city", "")
        priority_filter = f3.multiselect(
            "Priority", ["High", "Normal", "Low"], default=["High", "Normal"]
        )

        q = leads[leads["status"].isin(selected_status)].copy()
        if city_filter:
            q = q[q["city"].str.contains(city_filter, case=False, na=False)]
        q = q[q["priority"].isin(priority_filter)]

        st.write(f"**{len(q)} shops in queue**")

        for idx, row in q.iterrows():
            with st.container(border=True):
                a, b, c, d = st.columns([2.5, 1.3, 1.5, 1.5])
                a.markdown(f"**{row['shop_name']}**  \n{row['address']}")
                if row["phone"]:
                    b.markdown(f"[📞 {row['phone']}](tel:{row['phone']})")
                else:
                    b.write("No phone")

                new_status = c.selectbox(
                    "Status",
                    statuses,
                    index=statuses.index(row["status"]) if row["status"] in statuses else 0,
                    key=f"status_{idx}",
                )

                if d.button("Save", key=f"save_{idx}"):
                    leads.loc[idx, "status"] = new_status
                    leads.loc[idx, "last_called"] = str(date.today())
                    leads.loc[idx, "updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    save_df(leads, DATA_FILE)
                    st.rerun()

                note = st.text_input(
                    "Call note",
                    value=row["last_contact_note"],
                    key=f"note_{idx}",
                    placeholder="Example: Has 2 cats; call Friday after 10am",
                )
                cats = st.selectbox(
                    "Cats",
                    ["Unknown", "No", "Maybe", "Yes"],
                    index=["Unknown", "No", "Maybe", "Yes"].index(
                        row["cats_available"]
                    ) if row["cats_available"] in ["Unknown", "No", "Maybe", "Yes"] else 0,
                    key=f"cats_{idx}",
                )

                if st.button("Update call", key=f"update_{idx}"):
                    leads.loc[idx, "status"] = new_status
                    leads.loc[idx, "cats_available"] = cats
                    leads.loc[idx, "last_contact_note"] = note
                    leads.loc[idx, "last_called"] = str(date.today())
                    leads.loc[idx, "updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    save_df(leads, DATA_FILE)
                    st.rerun()

# ----------------------------------------------------------------------
# ROUTE
# ----------------------------------------------------------------------
with tabs[2]:
    st.subheader("Route candidates")
    st.caption(
        "Mark confirmed/likely suppliers as Route = Yes while you call. "
        "This becomes your pickup list."
    )

    if leads.empty:
        st.write("No leads yet.")
    else:
        route = leads[leads["route_flag"].eq("Yes")].copy()
        route = route[~route["status"].eq("Do Not Call")]

        st.write(f"**{len(route)} route stops**")

        if not route.empty:
            st.dataframe(
                route[
                    [
                        "priority", "shop_name", "phone", "address",
                        "status", "cats_available", "last_contact_note",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

            st.download_button(
                "Download route CSV",
                route.to_csv(index=False).encode("utf-8"),
                "today_route.csv",
                "text/csv",
            )

            for idx, row in route.iterrows():
                st.markdown(
                    f"**{row['shop_name']}** — "
                    f"[Apple Maps]({apple_maps_link(row['address'])}) · "
                    f"[Google Maps]({row['maps_url'] or google_maps_directions(row['address'])})"
                )

# ----------------------------------------------------------------------
# PRICE SHEET
# ----------------------------------------------------------------------
with tabs[3]:
    st.subheader("Refinery price sheet")
    st.caption(
        "Upload the weekly PDF from your refinery. It replaces the sheet "
        "below — search by cat code instead of digging through your Books app."
    )

    uploaded = st.file_uploader("Upload this week's price sheet (PDF)", type=["pdf"])

    if uploaded is not None:
        try:
            with st.spinner("Parsing price sheet..."):
                new_prices = parse_price_sheet_pdf(uploaded)
            prices = new_prices
            save_df(prices, PRICE_FILE)
            st.success(f"Loaded {len(prices)} cat codes from this sheet.")
        except Exception as exc:
            st.error(f"Couldn't parse that sheet: {exc}")

    if prices.empty:
        st.info("No price sheet loaded yet. Upload one above.")
    else:
        last_updated = prices["effective_date"].iloc[0] if len(prices) else ""
        st.caption(f"Currently loaded: {len(prices)} cat codes · uploaded {last_updated}")

        c1, c2 = st.columns([3, 1])
        code_search = c1.text_input(
            "Search cat code", "", placeholder="e.g. GM LG, ST EDGE, CHRYSLER 022AA"
        )
        target_multiple = c2.number_input(
            "Target multiple", min_value=1.0, value=2.5, step=0.1,
            help="Your minimum revenue multiple. Shown as a max-offer guide per code.",
        )

        if code_search:
            results = prices[
                prices["cat_code"].str.contains(code_search, case=False, na=False)
            ].copy()
        else:
            results = prices.copy()

        if code_search and results.empty:
            st.warning("No matching cat code found. Try a shorter fragment.")
        else:
            display = results.copy()
            numeric = pd.to_numeric(display["price_numeric"], errors="coerce")
            display["max_offer_at_target"] = numeric.apply(
                lambda v: f"${v / target_multiple:,.2f}" if pd.notna(v) else ""
            )
            st.dataframe(
                display[["cat_code", "price_display", "max_offer_at_target"]].rename(
                    columns={
                        "price_display": "refinery_price",
                        "max_offer_at_target": f"max_offer_{target_multiple}x",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        st.download_button(
            "Download full price sheet CSV",
            prices.to_csv(index=False).encode("utf-8"),
            "price_sheet.csv",
            "text/csv",
        )

# ----------------------------------------------------------------------
# TODAY'S RUN
# ----------------------------------------------------------------------
with tabs[4]:
    st.subheader("Today's cash + purchases")

    today = str(date.today())
    today_purchases = purchases[purchases["date"].eq(today)].copy()

    starting_cash = st.number_input(
        "Starting cash today",
        min_value=0.0,
        value=float(st.session_state.get("starting_cash", 0.0)),
        step=50.0,
    )
    st.session_state["starting_cash"] = starting_cash

    if not today_purchases.empty:
        spent = pd.to_numeric(today_purchases["amount_paid"], errors="coerce").fillna(0).sum()
        projected_profit = pd.to_numeric(
            today_purchases["projected_profit"], errors="coerce"
        ).fillna(0).sum()
        cats = pd.to_numeric(today_purchases["cat_count"], errors="coerce").fillna(0).sum()
    else:
        spent = projected_profit = cats = 0

    remaining = starting_cash - spent

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Starting cash", f"${starting_cash:,.2f}")
    m2.metric("Spent", f"${spent:,.2f}")
    m3.metric("Cash remaining", f"${remaining:,.2f}")
    m4.metric("Projected profit", f"${projected_profit:,.2f}")

    st.divider()
    st.subheader("Log a purchase")

    if not prices.empty:
        with st.expander("💵 Quick price check (before you log the buy)"):
            lookup = st.text_input("Cat code", "", key="purchase_price_lookup")
            if lookup:
                matches = prices[
                    prices["cat_code"].str.contains(lookup, case=False, na=False)
                ].copy()
                if matches.empty:
                    st.warning("No matching cat code.")
                else:
                    numeric = pd.to_numeric(matches["price_numeric"], errors="coerce")
                    matches["max_offer_2.5x"] = numeric.apply(
                        lambda v: f"${v / 2.5:,.2f}" if pd.notna(v) else ""
                    )
                    st.dataframe(
                        matches[["cat_code", "price_display", "max_offer_2.5x"]],
                        use_container_width=True,
                        hide_index=True,
                    )

    with st.form("purchase_form"):
        shop_options = leads["shop_name"].tolist() if not leads.empty else []
        shop = st.selectbox("Shop", ["—"] + shop_options)
        cat_count = st.number_input("Number of cats bought", min_value=1, step=1)
        amount = st.number_input("Cash paid", min_value=0.0, step=25.0)
        multiple = st.number_input(
            "Projected revenue multiple",
            min_value=1.0,
            value=2.5,
            step=0.1,
            help="Your stated minimum target is 2.5x purchase cost.",
        )
        note = st.text_input("Purchase note", placeholder="Types/grades, condition, anything useful")
        submitted = st.form_submit_button("Save purchase", type="primary")

        if submitted:
            if shop == "—":
                st.error("Select a shop.")
            else:
                shop_row = leads[leads["shop_name"].eq(shop)].iloc[0]
                revenue = float(amount) * float(multiple)
                profit = revenue - float(amount)
                purchase_id = datetime.now().strftime("%Y%m%d%H%M%S%f")

                new_purchase = {
                    "purchase_id": purchase_id,
                    "date": today,
                    "shop_name": shop,
                    "place_id": shop_row["place_id"],
                    "cat_count": int(cat_count),
                    "amount_paid": float(amount),
                    "projected_revenue": revenue,
                    "projected_profit": profit,
                    "multiple": multiple,
                    "notes": note,
                }

                purchases = pd.concat(
                    [purchases, pd.DataFrame([new_purchase])],
                    ignore_index=True,
                )
                save_df(purchases, PURCHASE_FILE)

                i = leads.index[leads["place_id"].eq(shop_row["place_id"])]
                if len(i):
                    i = i[0]
                    leads.loc[i, "last_purchase_date"] = today
                    leads.loc[i, "purchase_count"] = str(
                        int(float(leads.loc[i, "purchase_count"] or 0)) + 1
                    )
                    leads.loc[i, "total_cats_bought"] = str(
                        int(float(leads.loc[i, "total_cats_bought"] or 0)) + int(cat_count)
                    )
                    leads.loc[i, "total_spent"] = str(
                        float(leads.loc[i, "total_spent"] or 0) + float(amount)
                    )
                    leads.loc[i, "status"] = "Purchased Before"
                    leads.loc[i, "cats_available"] = "Unknown"
                    leads.loc[i, "updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                    save_df(leads, DATA_FILE)

                st.success(
                    f"Purchase saved. Projected revenue ${revenue:,.2f}; "
                    f"projected profit ${profit:,.2f}."
                )
                st.rerun()

    st.subheader("Today's purchases")
    if today_purchases.empty:
        st.write("No purchases logged today.")
    else:
        st.dataframe(today_purchases, use_container_width=True, hide_index=True)

# ----------------------------------------------------------------------
# CRM
# ----------------------------------------------------------------------
with tabs[5]:
    st.subheader("Supplier CRM")

    if leads.empty:
        st.write("No suppliers yet.")
    else:
        search = st.text_input("Search supplier", "")
        if search:
            view = leads[
                leads["shop_name"].str.contains(search, case=False, na=False)
                | leads["city"].str.contains(search, case=False, na=False)
            ].copy()
        else:
            view = leads.copy()

        st.dataframe(
            view[
                [
                    "priority", "shop_name", "city", "phone", "status",
                    "cats_available", "last_called", "last_purchase_date",
                    "purchase_count", "total_cats_bought", "total_spent",
                    "route_flag", "last_contact_note",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        st.download_button(
            "Export full CRM",
            view.to_csv(index=False).encode("utf-8"),
            "catalytic_converter_supplier_crm.csv",
            "text/csv",
        )

# ----------------------------------------------------------------------
# STATS
# ----------------------------------------------------------------------
with tabs[6]:
    st.subheader("Business stats")

    if leads.empty:
        st.write("No CRM data yet.")
    else:
        total = len(leads)
        called = (leads["last_called"].astype(str).str.len() > 0).sum()
        suppliers = (leads["purchase_count"].astype(float) > 0).sum()
        route_count = (leads["route_flag"] == "Yes").sum()

        a, b, c, d = st.columns(4)
        a.metric("Total shops", total)
        b.metric("Called", int(called))
        c.metric("Suppliers bought from", int(suppliers))
        d.metric("Route candidates", int(route_count))

        if not purchases.empty:
            p = purchases.copy()
            p["amount_paid"] = pd.to_numeric(p["amount_paid"], errors="coerce").fillna(0)
            p["projected_profit"] = pd.to_numeric(
                p["projected_profit"], errors="coerce"
            ).fillna(0)
            p["cat_count"] = pd.to_numeric(p["cat_count"], errors="coerce").fillna(0)

            st.metric("All-time cash spent", f"${p['amount_paid'].sum():,.2f}")
            st.metric("All-time projected profit", f"${p['projected_profit'].sum():,.2f}")
            st.metric("All-time cats purchased", int(p["cat_count"].sum()))

            by_shop = (
                p.groupby("shop_name", as_index=False)
                .agg(
                    purchases=("purchase_id", "count"),
                    cats=("cat_count", "sum"),
                    spent=("amount_paid", "sum"),
                    projected_profit=("projected_profit", "sum"),
                )
                .sort_values("projected_profit", ascending=False)
            )
            st.dataframe(by_shop, use_container_width=True, hide_index=True)
