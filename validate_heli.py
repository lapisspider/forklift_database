"""Validation script for Heli import data integrity."""
import sqlite3

def main():
    db = sqlite3.connect('data/forklifts.db')
    cur = db.cursor()

    print("=== Heli Import Validation ===\n")

    # Count total Heli models
    cur.execute("SELECT COUNT(*) FROM forklifts WHERE manufacturer='Heli'")
    total = cur.fetchone()[0]
    print(f"Total Heli models: {total}")

    # Check for nulls in critical fields
    for field in ['series', 'truck_class', 'fuel_type']:
        cur.execute(f"SELECT COUNT(*) FROM forklifts WHERE manufacturer='Heli' AND {field} IS NULL")
        nulls = cur.fetchone()[0]
        if nulls > 0:
            print(f"⚠️  {nulls} rows with NULL {field}")

    # Check series consistency (no "Series" word)
    cur.execute("SELECT DISTINCT series FROM forklifts WHERE manufacturer='Heli' AND series LIKE '%Series%'")
    bad_series = cur.fetchall()
    if bad_series:
        print(f"⚠️  {len(bad_series)} series contain 'Series' word: {bad_series}")

    # Check fuel_type is canonical
    canonical_fuels = {'Electric', 'Diesel', 'LPG', 'Gasoline', 'Gasoline/LPG'}
    cur.execute("SELECT DISTINCT fuel_type FROM forklifts WHERE manufacturer='Heli'")
    found_fuels = {f[0] for f in cur.fetchall() if f[0]}
    bad_fuels = found_fuels - canonical_fuels
    if bad_fuels:
        print(f"⚠️  Non-canonical fuel types: {bad_fuels}")
    else:
        print(f"✓ Fuel types canonical: {found_fuels}")

    # Check Class field is set
    cur.execute("SELECT COUNT(*) FROM forklifts WHERE manufacturer='Heli' AND truck_class IS NULL")
    nulls = cur.fetchone()[0]
    if nulls > 0:
        print(f"⚠️  {nulls} rows missing truck_class (OSHA class)")
    else:
        print("✓ All rows have truck_class")

    # Sample a few rows
    print("\nSample Heli rows:")
    cur.execute("""
        SELECT id, model, series, capacity_kg, fuel_type, truck_class, transmission_notes
        FROM (
            SELECT id, model, series, capacity_kg, fuel_type, truck_class,
                   SUBSTR(notes, 1, 40) as transmission_notes
            FROM forklifts WHERE manufacturer='Heli' LIMIT 5
        )
    """)
    for row in cur.fetchall():
        print(f"  {row}")

    db.close()

if __name__ == "__main__":
    main()
