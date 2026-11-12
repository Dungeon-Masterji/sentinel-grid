"""Generate Synthea geography/demographics resources for the UAE.

This is the single reviewable source of every demographic assumption. It writes
into configs/synthea/resources/ (tracked in git), mirroring the layout of
synthea/src/main/resources/. packages/pipelines/synth/synthea_wrapper.py copies
that tree into the Synthea checkout before each run (synthea/ is gitignored).

    python configs/synthea/build_uae_geography.py

Why the output looks the way it does
------------------------------------
Synthea's Demographics loader (Demographics.csvLineToDemographics) only reads a
fixed US-Census-shaped schema:
  ID, NAME, STNAME, CTYNAME, POPESTIMATE2015, TOT_MALE, TOT_FEMALE,
  WHITE/BLACK/ASIAN/NATIVE/OTHER, HISPANIC, 1..18 (5-year age bands),
  00..10 ... 200..999 (income, $k), LESS_THAN_HS/HS_DEGREE/SOME_COLLEGE/BS_DEGREE
So the UAE-specific buckets below (9 age bands, 6 ethnicities) are *inputs*: they
are mapped onto Synthea's columns, and the UAE ethnicity shares are also written
as extra columns (Synthea ignores unknown columns) so downstream code can use them.

Ethnicity -> Synthea race mapping (US Census convention; MENA counts as WHITE):
  EMIRATI, OTHER_ARAB, WESTERN -> WHITE
  SOUTH_ASIAN, EAST_ASIAN      -> ASIAN
  AFRICAN                      -> BLACK
  HISPANIC = 0 everywhere.

Sources / plausibility notes (approximate, for synthetic generation only):
  - Populations: rounded from Dubai Statistics Center 2024, SCAD 2023 (Abu Dhabi,
    Al Ain), and emirate statistics offices; suburb figures (Khalifa City,
    MBZ City) are order-of-magnitude estimates.
  - Coordinates: OpenStreetMap place features via Nominatim (osm ids in CITIES).
  - Age: UAE pyramid is dominated by working-age expats; 35-44 ~30%, 65+ <2%.
  - Sex: ~70% male nationally, driven by expat labour; residential suburbs lower.
  - Ethnicity: national ~12/60/15/5/5/3 (Emirati/South Asian/Other Arab/Western/
    East Asian/African), shifted per city where Emiratis are a larger share.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
RES = HERE / "resources"

COUNTRY = "United Arab Emirates"
COUNTRY_ABBR = "AE"

# name, emirate (county), lat, lon, population, osm ref
CITIES = [
    ("Dubai", "Dubai", 25.2647227, 55.2924146, 3_800_000, "node/31248510"),
    ("Abu Dhabi", "Abu Dhabi", 24.4538352, 54.3774014, 1_600_000, "relation/4479763"),
    ("Sharjah", "Sharjah", 25.3461498, 55.4210633, 1_800_000, "relation/12905785"),
    ("Ajman", "Ajman", 25.3936557, 55.4451432, 540_000, "node/262479959"),
    ("Ras Al Khaimah", "Ras Al Khaimah", 25.7737705, 55.9382320, 400_000, "node/262479317"),
    ("Fujairah", "Fujairah", 25.1244604, 56.3355085, 150_000, "way/872858707"),
    ("Umm Al Quwain", "Umm Al Quwain", 25.5520435, 55.5474977, 85_000, "node/896422314"),
    ("Al Ain", "Abu Dhabi", 24.2248697, 55.7452211, 850_000, "node/82799640"),
    ("Khalifa City", "Abu Dhabi", 24.4201314, 54.5749535, 140_000, "relation/13251355"),
    ("Mohammed Bin Zayed City", "Abu Dhabi", 24.3339589, 54.5535564, 270_000, "way/873221076"),
]

# UAE age buckets (spec). Sum = 1.0. 35-44 is the modal bucket at 30%; 65+ = 1.6%.
AGE_BUCKETS = {
    "0-4": 0.050, "5-14": 0.090, "15-24": 0.090, "25-34": 0.240, "35-44": 0.300,
    "45-54": 0.150, "55-64": 0.064, "65-74": 0.013, "75+": 0.003,
}
# Split of each UAE bucket into Synthea's 5-year bands (columns "1".."18").
AGE_TO_SYNTHEA = {
    "0-4": [("1", 1.0)],
    "5-14": [("2", 0.5), ("3", 0.5)],
    "15-24": [("4", 0.40), ("5", 0.60)],          # tilt to 20-24 (labour inflow)
    "25-34": [("6", 0.46), ("7", 0.54)],
    "35-44": [("8", 0.50), ("9", 0.50)],
    "45-54": [("10", 0.57), ("11", 0.43)],
    "55-64": [("12", 0.62), ("13", 0.38)],
    "65-74": [("14", 0.65), ("15", 0.35)],
    "75+": [("16", 0.60), ("17", 0.27), ("18", 0.13)],
}

ETHNICITIES = ["EMIRATI", "SOUTH_ASIAN", "OTHER_ARAB", "WESTERN", "EAST_ASIAN", "AFRICAN"]
NATIONAL_ETHNICITY = dict(zip(ETHNICITIES, [0.12, 0.60, 0.15, 0.05, 0.05, 0.03]))
ETHNICITY_OVERRIDES = {
    "Abu Dhabi": [0.18, 0.52, 0.16, 0.07, 0.04, 0.03],
    "Al Ain": [0.30, 0.45, 0.17, 0.02, 0.03, 0.03],
    "Khalifa City": [0.40, 0.25, 0.20, 0.08, 0.04, 0.03],
    "Mohammed Bin Zayed City": [0.25, 0.42, 0.20, 0.05, 0.05, 0.03],
    "Ras Al Khaimah": [0.20, 0.52, 0.16, 0.05, 0.04, 0.03],
    "Fujairah": [0.22, 0.52, 0.15, 0.03, 0.04, 0.04],
    "Umm Al Quwain": [0.18, 0.55, 0.16, 0.03, 0.04, 0.04],
    "Dubai": [0.08, 0.62, 0.13, 0.07, 0.07, 0.03],
}
RACE_OF = {
    "EMIRATI": "WHITE", "OTHER_ARAB": "WHITE", "WESTERN": "WHITE",
    "SOUTH_ASIAN": "ASIAN", "EAST_ASIAN": "ASIAN", "AFRICAN": "BLACK",
}

MALE_SHARE = {
    "Dubai": 0.72, "Abu Dhabi": 0.71, "Sharjah": 0.68, "Ajman": 0.67,
    "Ras Al Khaimah": 0.63, "Fujairah": 0.62, "Umm Al Quwain": 0.64,
    "Al Ain": 0.63, "Khalifa City": 0.56, "Mohammed Bin Zayed City": 0.62,
}

# Income: log-normal annual household income (USD), sigma 1.0, per-city median.
# Population-weighted national median lands ~ $60k.
INCOME_MEDIAN_USD = {
    "Dubai": 66_000, "Abu Dhabi": 72_000, "Sharjah": 48_000, "Ajman": 40_000,
    "Ras Al Khaimah": 44_000, "Fujairah": 40_000, "Umm Al Quwain": 40_000,
    "Al Ain": 52_000, "Khalifa City": 90_000, "Mohammed Bin Zayed City": 62_000,
}
INCOME_SIGMA = 1.0
INCOME_EDGES_K = [0, 10, 15, 25, 35, 50, 75, 100, 150, 200, math.inf]
INCOME_COLS = ["00..10", "10..15", "15..25", "25..35", "35..50", "50..75",
               "75..100", "100..150", "150..200", "200..999"]

# Education (adults): bimodal — large low-education labour force + graduate expats.
EDUCATION = {"LESS_THAN_HS": 0.27, "HS_DEGREE": 0.33, "SOME_COLLEGE": 0.12, "BS_DEGREE": 0.28}


def _lognorm_cdf(x: float, median: float, sigma: float) -> float:
    if x <= 0:
        return 0.0
    if math.isinf(x):
        return 1.0
    return 0.5 * (1 + math.erf(math.log(x / median) / (sigma * math.sqrt(2))))


def income_buckets(median_usd: float) -> dict[str, float]:
    out = {}
    for col, lo, hi in zip(INCOME_COLS, INCOME_EDGES_K[:-1], INCOME_EDGES_K[1:]):
        out[col] = _lognorm_cdf(hi * 1000, median_usd, INCOME_SIGMA) - _lognorm_cdf(
            lo * 1000, median_usd, INCOME_SIGMA)
    return out


def city_id(i: int) -> str:
    return f"AE{i + 1:03d}"


PROVIDER_HEADER = ["provider_num", "npi", "name", "address", "city", "state", "zip",
                   "fips_county", "lat", "lon", "phone", "provider_type_code", "category",
                   "emergency", "upin", "pin", "region_code", "bed_count", "clia_lab_number"]
# file stem -> (label, emergency flag, bed count, only in cities above this population)
PROVIDER_KINDS = {
    "hospitals": ("General Hospital", "true", 300, 0),
    "primary_care_facilities": ("Primary Health Centre", "false", "", 0),
    "urgent_care_facilities": ("Urgent Care Clinic", "true", "", 0),
    "dialysis": ("Dialysis Centre", "false", "", 300_000),
    "rehab": ("Rehabilitation Centre", "false", 60, 300_000),
    "longterm": ("Long-Term Care Centre", "false", 80, 300_000),
    "nursing": ("Nursing Home", "false", 80, 300_000),
    "hospice": ("Palliative Care Unit", "false", 20, 300_000),
    "home_health_agencies": ("Home Health Service", "false", "", 300_000),
}
EMPTY_PROVIDER_FILES = ["va_facilities", "ihs_facilities", "ihs_centers"]


def write_providers() -> None:
    pdir = RES / "providers" / "uae"
    pdir.mkdir(parents=True, exist_ok=True)
    for k, (stem, (label, emergency, beds, min_pop)) in enumerate(PROVIDER_KINDS.items()):
        with open(pdir / f"{stem}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(PROVIDER_HEADER)
            for i, (name, _, lat, lon, pop, _) in enumerate(CITIES):
                if pop < min_pop:
                    continue
                num = f"AE{k:02d}{i:03d}"
                # small deterministic offset so facilities aren't exactly at the city centroid
                dlat, dlon = 0.004 * ((k % 3) - 1), 0.004 * ((k % 5) - 2)
                w.writerow([num, f"9{k:02d}{i:07d}", f"{name} {label} (Synthetic)",
                            f"{name}, {COUNTRY}", name, COUNTRY_ABBR, f"{i + 1:05d}",
                            f"{i + 1:03d}", round(lat + dlat, 6), round(lon + dlon, 6),
                            "", "", "", emergency, "", "", "", beds, ""])
    for stem in EMPTY_PROVIDER_FILES:
        with open(pdir / f"{stem}.csv", "w", newline="") as f:
            csv.writer(f).writerow(PROVIDER_HEADER)


# Payers. Synthea hard-requires Medicare/Medicaid/Dual Eligible companies and a
# per-state row in the Medicaid eligibility files. The UAE has no Medicaid; we
# keep those government plans structurally (eligibility thresholds of 0 so they
# rarely trigger) and replace US private/ACA plans with mandatory-employer plans,
# which is how most UAE residents are covered (Dubai ISAHD, Abu Dhabi Daman/Thiqa).
PAYER_COMPANIES = """Id,Name,Address,City,State Headquarterd,Zip,Phone,States Covered,Ownership
10000,Medicare,n/a,n/a,AE,00000,n/a,*,Government
20000,Medicaid,n/a,n/a,AE,00000,n/a,*,Government
30000,Dual Eligible,n/a,n/a,AE,00000,n/a,*,Government
41000,UAE Government Health Programme (Synthetic),n/a,Abu Dhabi,AE,00002,n/a,*,Government
42000,UAE Employer Health Insurer (Synthetic),n/a,Dubai,AE,00001,n/a,*,Private
"""
PAYER_PLANS = """Payer Id,Plan Id,Name,Services Covered,Deductible,Default Coinsurance,Default Copay,Monthly Premium,Max Out of Pocket,Medicare Supplement,Is ACA Plan,Income Based Premium,Eligibility Policy,Start Year,End Year,Priority Level,Notes
10000,10001,Medicare Plan,*,233,0.8,0,170.10,8700,false,false,false,MedicareEligible,0,,2,
20000,20001,Medicaid Plan,*,100,0,50,10,8700,false,false,false,MedicaidEligible,0,,2,
30000,30001,Standard Dual Eligible Plan,*,0,0,35,10,8700,false,false,false,DualEligible,0,,1,
41000,41001,Government Health Programme (Synthetic),*,0,1.0,0,0,0,false,false,false,Generic,0,,,
42000,42001,Essential Benefits Plan (Synthetic),*,0,0.8,10,60,2000,false,false,false,Generic,0,,,
42000,42002,Enhanced Plan (Synthetic),*,0,0.9,5,220,1500,false,false,false,Generic,0,,,
"""


def write_payers(synthea_resources: Path | None = None) -> None:
    """Payer files. US eligibility inputs are copied from the Synthea checkout (if
    present) with a UAE row appended; otherwise a minimal UAE-only file is written."""
    pdir = RES / "payers" / "uae"
    edir = pdir / "eligibility_input_files"
    edir.mkdir(parents=True, exist_ok=True)
    (pdir / "insurance_companies.csv").write_text(PAYER_COMPANIES)
    (pdir / "insurance_plans.csv").write_text(PAYER_PLANS)
    (pdir / "insurance_eligibilities.csv").write_text(
        "Name,Poverty Multiplier,Income Threshold,Age Threshold,Qualifying Codes,"
        "Qualifying Attributes,Poverty Multiplier File,Spenddown File,Acceptance Likelihood,"
        "Sub-Eligibilities,Logical Operator,Notes\n"
        "StandardSocialSecurityDisability,,,,payers/uae/eligibility_input_files/ssd_eligibility.csv,,,,0.85,,and,\n"
        "MedicareEligible,,,65,,end_stage_renal_disease = true,,,,StandardSocialSecurityDisability,or,\n"
        "MedicaidEligible,,,,,blindness = true,payers/uae/eligibility_input_files/medicaid_income_eligibilities.csv,"
        "payers/uae/eligibility_input_files/medicaid_mnil_eligibilities.csv,,,or,\n"
        "DualEligible,,,,,,,,0.85,MedicareEligible|MedicaidEligible,and,\n")
    (edir / "medicaid_income_eligibilities.csv").write_text(
        f"state,0-1,2-5,6-18,pregnant,parent,adult\n{COUNTRY},0,0,0,0,0,0\n")
    (edir / "medicaid_mnil_eligibilities.csv").write_text(
        f"state,spenddown-available,age-blind-disabled-limit,monthly-spenddown-req\n{COUNTRY},FALSE,na,na\n")
    ssd_src = (synthea_resources or Path("/nonexistent")) / "payers/eligibility_input_files/ssd_eligibility.csv"
    if ssd_src.exists():
        (edir / "ssd_eligibility.csv").write_bytes(ssd_src.read_bytes())
    elif not (edir / "ssd_eligibility.csv").exists():
        (edir / "ssd_eligibility.csv").write_text("condition_name,attribute,codes\n")


def build() -> None:
    assert abs(sum(AGE_BUCKETS.values()) - 1) < 1e-9
    assert abs(sum(NATIONAL_ETHNICITY.values()) - 1) < 1e-9
    (RES / "geography" / "demographics").mkdir(parents=True, exist_ok=True)

    # 1. Spec-format city list (human readable; not read by Synthea directly).
    with open(RES / "geography" / "uae_cities.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["STATE", "CITY", "LATITUDE", "LONGITUDE", "POPULATION"])
        for name, _, lat, lon, pop, _ in CITIES:
            w.writerow([COUNTRY, name, lat, lon, pop])

    # 2. Synthea-schema demographics (+ UAE extra columns).
    synthea_ages = {}
    for bucket, share in AGE_BUCKETS.items():
        for col, frac in AGE_TO_SYNTHEA[bucket]:
            synthea_ages[col] = synthea_ages.get(col, 0.0) + share * frac
    assert abs(sum(synthea_ages.values()) - 1) < 1e-9

    header = (["ID", "COUNTY", "NAME", "STNAME", "POPESTIMATE2015", "CTYNAME", "TOT_POP",
               "TOT_MALE", "TOT_FEMALE", "WHITE", "HISPANIC", "BLACK", "ASIAN", "NATIVE",
               "OTHER"]
              + [str(i) for i in range(1, 19)] + INCOME_COLS + list(EDUCATION)
              + [f"UAE_AGE_{k}" for k in AGE_BUCKETS] + ETHNICITIES)
    with open(RES / "geography" / "demographics" / "uae_demographics.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i, (name, emirate, _, _, pop, _) in enumerate(CITIES):
            eth = dict(zip(ETHNICITIES, ETHNICITY_OVERRIDES.get(name, NATIONAL_ETHNICITY.values())))
            assert abs(sum(eth.values()) - 1) < 1e-9, name
            race = {"WHITE": 0.0, "BLACK": 0.0, "ASIAN": 0.0, "NATIVE": 0.0, "OTHER": 0.0}
            for e, s in eth.items():
                race[RACE_OF[e]] += s
            male = MALE_SHARE[name]
            inc = income_buckets(INCOME_MEDIAN_USD[name])
            row = ([city_id(i), f"{i + 1:03d}", name, COUNTRY, pop, emirate, pop,
                    male, round(1 - male, 6), round(race["WHITE"], 6), 0.0,
                    round(race["BLACK"], 6), round(race["ASIAN"], 6), 0.0, 0.0]
                   + [round(synthea_ages[str(c)], 6) for c in range(1, 19)]
                   + [round(inc[c], 6) for c in INCOME_COLS]
                   + list(EDUCATION.values())
                   + list(AGE_BUCKETS.values())
                   + [eth[e] for e in ETHNICITIES])
            w.writerow(row)

    # 3. zipcodes.csv equivalent: Synthea places people at city lat/lon from here.
    with open(RES / "geography" / "uae_zipcodes.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["", "USPS", "ST", "NAME", "ZCTA5", "LAT", "LON"])
        for i, (name, _, lat, lon, _, _) in enumerate(CITIES):
            w.writerow(["", COUNTRY, COUNTRY_ABBR, name, f"{i + 1:05d}", lat, lon])

    # 4. Timezones (UAE is UTC+4, no DST). Synthea still emits UTC timestamps.
    with open(RES / "geography" / "uae_timezones.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["STATE", "ST", "TIMEZONE", "TZ"])
        w.writerow([COUNTRY, COUNTRY_ABBR, "Gulf Standard Time", "GST"])

    # 5. Providers. Synthea filters provider files by state abbreviation, so the
    #    US files yield nothing for AE. Facilities are deliberately generic and
    #    labelled synthetic so no synthetic record is attributed to a real hospital.
    write_providers()
    write_payers(HERE.parents[1] / "synthea" / "src" / "main" / "resources")

    # Sanity print
    tot = sum(c[4] for c in CITIES)
    male = sum(MALE_SHARE[c[0]] * c[4] for c in CITIES) / tot
    med = sum(INCOME_MEDIAN_USD[c[0]] * c[4] for c in CITIES) / tot
    print(f"cities={len(CITIES)} pop={tot:,} male={male:.3f} pop-weighted median income=${med:,.0f}")
    print(f"35-44 share={AGE_BUCKETS['35-44']:.2f} 65+ share="
          f"{AGE_BUCKETS['65-74'] + AGE_BUCKETS['75+']:.3f}")


if __name__ == "__main__":
    build()
