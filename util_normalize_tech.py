"""
Normalize Tech_Mentioned and Tech_Assumed — Deterministic canonical mapping, no LLM.

Deduplicates and standardizes technology terms:
  "AI, Machine Learning, Artificial Intelligence" → "AI, ML"
  "machine learning algorithms" → "ML"
  "Natural Language Processing" → "NLP"

Usage:
    python3 util_normalize_tech.py           # Dry run
    python3 util_normalize_tech.py --commit  # Apply changes
"""
import argparse
import sqlite3
from agent_core import get_db_connection

DB_NAME = "Master.db"

# ── Canonical Mapping ────────────────────────────────────────────────────────
# Keys are lowercased for matching. Values are the canonical form.

TECH_CANONICAL = {
    # AI variants
    "ai": "AI",
    "artificial intelligence": "AI",
    "artificial intelligence (ai)": "AI",
    "a.i.": "AI",
    "a.i": "AI",
    # ML variants
    "machine learning": "ML",
    "machine learning algorithms": "ML",
    "ml": "ML",
    "ml algorithms": "ML",
    # Deep Learning
    "deep learning": "Deep Learning",
    "dl": "Deep Learning",
    # NLP variants
    "natural language processing": "NLP",
    "natural language processing (nlp)": "NLP",
    "nlp": "NLP",
    "natural language understanding": "NLP",
    "nlu": "NLP",
    # Computer Vision
    "computer vision": "Computer Vision",
    "cv": "Computer Vision",
    "image recognition": "Computer Vision",
    "object detection": "Computer Vision",
    # IoT
    "internet of things": "IoT",
    "iot": "IoT",
    # Blockchain
    "blockchain": "Blockchain",
    "blockchain technology": "Blockchain",
    # Cloud / Infra
    "cloud computing": "Cloud",
    "cloud": "Cloud",
    # Big Data
    "big data": "Big Data",
    "bigdata": "Big Data",
    # Robotics
    "robotics": "Robotics",
    "robotic process automation": "RPA",
    "rpa": "RPA",
    # Generative AI
    "generative ai": "Generative AI",
    "genai": "Generative AI",
    "gen ai": "Generative AI",
    "large language model": "LLM",
    "large language models": "LLM",
    "llm": "LLM",
    "llms": "LLM",
    # Frameworks (keep as-is but normalize casing)
    "tensorflow": "TensorFlow",
    "pytorch": "PyTorch",
    "keras": "Keras",
    "scikit-learn": "scikit-learn",
    "opencv": "OpenCV",
    "hugging face": "Hugging Face",
    "huggingface": "Hugging Face",
    "langchain": "LangChain",
    # Languages (keep as-is)
    "python": "Python",
    "javascript": "JavaScript",
    "typescript": "TypeScript",
    "java": "Java",
    "c++": "C++",
    "rust": "Rust",
    "go": "Go",
    "r": "R",
    "sql": "SQL",
    # Specific tools
    "react": "React",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "k8s": "Kubernetes",
    "aws": "AWS",
    "azure": "Azure",
    "gcp": "GCP",
    "google cloud": "GCP",
    # YOLO variants
    "yolov5": "YOLOv5",
    "yolov8": "YOLOv8",
    "yolo": "YOLO",
    # Business model terms that leaked into tech fields — discard
    "saas": None,
    "platform": None,
    "api": None,
    "algorithms": None,
    "software": None,
    "ai-powered": None,
    "ai powered": None,
    "data analytics": "Data Analytics",
    "data analysis": "Data Analytics",
    "predictive analytics": "Predictive Analytics",
    "predictive modelling": "Predictive Analytics",
    "predictive modeling": "Predictive Analytics",
    "speech recognition": "Speech Recognition",
    "speech-to-text": "Speech Recognition",
    "recommendation system": "Recommendation Systems",
    "recommendation systems": "Recommendation Systems",
    "amazon web services": "AWS",
    "microsoft azure": "Azure",
    "augmented reality": "AR",
    "virtual reality": "VR",
    "augmented reality (ar)": "AR",
    "virtual reality (vr)": "VR",
}


def normalize_tech_field(value):
    """Normalize a comma-separated tech field.
    Returns (normalized_string, changed_bool)."""
    if not value or str(value).strip().lower() in ("none", "unknown", "nan", ""):
        return value, False

    # Split by comma, normalize each term
    terms = [t.strip() for t in str(value).split(",") if t.strip()]
    normalized = []
    seen = set()

    for term in terms:
        key = term.lower().strip()
        if key in TECH_CANONICAL:
            canonical = TECH_CANONICAL[key]
            if canonical is None:  # explicitly discard this term
                continue
        else:
            canonical = term.strip()

        # Deduplicate (case-insensitive)
        if canonical.lower() not in seen:
            seen.add(canonical.lower())
            normalized.append(canonical)

    result = ", ".join(sorted(normalized))
    changed = result != value
    return result, changed


def normalize_all(conn, field_name, commit=False):
    """Normalize a tech field across all startups."""
    cursor = conn.cursor()
    rows = cursor.execute(f"""
        SELECT id, company_name, {field_name}
        FROM startups
        WHERE {field_name} IS NOT NULL AND {field_name} != '' AND {field_name} != 'None'
    """).fetchall()

    changed_count = 0
    for sid, name, value in rows:
        normalized, changed = normalize_tech_field(value)
        if changed:
            changed_count += 1
            if changed_count <= 30:  # Show first 30 changes
                print(f"  [{sid}] {name}: '{value}' → '{normalized}'")
            if commit:
                cursor.execute(f"UPDATE startups SET {field_name} = ? WHERE id = ?", (normalized, sid))

    if commit:
        conn.commit()

    return changed_count, len(rows)


def main():
    parser = argparse.ArgumentParser(description="Normalize Tech_Mentioned and Tech_Assumed")
    parser.add_argument("--commit", action="store_true", help="Apply changes (default: dry run)")
    args = parser.parse_args()

    conn = get_db_connection(DB_NAME)
    conn.execute("PRAGMA busy_timeout = 30000")

    print("=" * 60)
    print(f"  Normalize Tech Fields ({'COMMIT' if args.commit else 'DRY RUN'})")
    print("=" * 60)

    print("\n--- Tech_Mentioned ---")
    changed_m, total_m = normalize_all(conn, "Tech_Mentioned", commit=args.commit)
    print(f"\n  Changed: {changed_m} / {total_m}")

    print("\n--- Tech_Assumed ---")
    changed_a, total_a = normalize_all(conn, "Tech_Assumed", commit=args.commit)
    print(f"\n  Changed: {changed_a} / {total_a}")

    # Show post-normalization top terms
    if args.commit:
        cursor = conn.cursor()
        print("\n--- Post-Normalization Top Tech_Mentioned ---")
        rows = cursor.execute("""
            SELECT Tech_Mentioned, COUNT(*) as cnt
            FROM startups
            WHERE Tech_Mentioned IS NOT NULL AND Tech_Mentioned != '' AND Tech_Mentioned != 'None'
            GROUP BY Tech_Mentioned ORDER BY cnt DESC LIMIT 15
        """).fetchall()
        for tech, cnt in rows:
            print(f"  [{cnt:>4}] {tech}")

    print(f"\n{'=' * 60}")
    print(f"  Total: {changed_m + changed_a} fields normalized")
    if not args.commit:
        print("  ⚠ DRY RUN — no changes written. Use --commit to apply.")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
