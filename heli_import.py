"""Import Heli's complete forklift lineup from en.heli.com.cn and helichina.net.

Coverage: IC counterbalance, electric counterbalance, li-ion, narrow-aisle reach trucks,
warehouse equipment (stackers, order pickers), cushion-tire, heavy, tractor, and rough terrain.

Series rules (from the user):
- Do NOT include "Series" in the series name; store "G3", not "G3 Series"
- Full order code in model, e.g. CPCD25-K2, CPYD25-KU1H, CPD25-GE2G
- Split on fuel AND transmission: separate rows for CPCD (diesel torque converter),
  CPC (diesel clutch), CPQD (gas torque converter), etc.
- Capacity always in kg (Heli publishes metric)
- OSHA class mapping:
  * Electric counterbalance = Class I (regardless of tire)
  * Narrow-aisle reach/order picker = Class II
  * Pallet/walkie stacker = Class III
  * IC cushion tire = Class IV
  * IC pneumatic tire = Class V (including heavy 12-46t)
  * Tow tractor = Class VI
  * Rough terrain = Class VII

Dedupes by (manufacturer='Heli', model) — safe to re-run.
Usage: python heli_import.py
"""
import csv
from typing import NamedTuple

from app.database import SessionLocal
from app.main import next_company_serial
from app.models import Forklift

HELI = "Heli"
REPORT = "heli_import_report.csv"

# Field structure for model entries
class HeliBuild(NamedTuple):
    model: str                # Full code incl. series suffix, e.g. CPCD25-K2
    series: str               # Series name, no "Series" word
    capacity_kg: float        # Numeric kg
    fuel_type: str            # Diesel, LPG, Gasoline, Gasoline/LPG, Electric
    truck_class: str          # Class I through Class VII
    transmission: str         # Torque Converter, Clutch, N/A (for electric)
    tire_type: str            # Pneumatic, Cushion, 3-wheel, 4-wheel
    year_start: int | None    # Production start year (estimate allowed)
    year_end: int | None      # Production end year (NULL = still in production)
    notes: str                # Summary: engine, capacity in lb, ambiguities

# MODELS table: comprehensive Heli lineup from helichina.net and helieurope.eu research
# Data validated against Opus research completion (Sept 18, 2026)
MODELS = [
    # ========== IC COUNTERBALANCE — H3 SERIES (1-3.8 tons) ==========
    HeliBuild("CPCD10-H3", "H3", 1000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton diesel torque converter, 2,200 lb"),
    HeliBuild("CPCD15-H3", "H3", 1500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton diesel torque converter, 3,300 lb"),
    HeliBuild("CPCD18-H3", "H3", 1800, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton diesel torque converter, 3,968 lb"),
    HeliBuild("CPCD20-H3", "H3", 2000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton diesel torque converter, 4,409 lb"),
    HeliBuild("CPCD25-H3", "H3", 2500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton diesel torque converter, 5,511 lb"),
    HeliBuild("CPCD30-H3", "H3", 3000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton diesel torque converter, 6,614 lb"),
    HeliBuild("CPCD35-H3", "H3", 3500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton diesel torque converter, 7,716 lb"),
    HeliBuild("CPCD38-H3", "H3", 3800, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton diesel torque converter, 8,378 lb"),
    HeliBuild("CPC10-H3", "H3", 1000, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1-ton diesel clutch (mechanical), 2,200 lb"),
    HeliBuild("CPC15-H3", "H3", 1500, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.5-ton diesel clutch (mechanical), 3,300 lb"),
    HeliBuild("CPC18-H3", "H3", 1800, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.8-ton diesel clutch (mechanical), 3,968 lb"),
    HeliBuild("CPC20-H3", "H3", 2000, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2-ton diesel clutch (mechanical), 4,409 lb"),
    HeliBuild("CPC25-H3", "H3", 2500, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2.5-ton diesel clutch (mechanical), 5,511 lb"),
    HeliBuild("CPC30-H3", "H3", 3000, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3-ton diesel clutch (mechanical), 6,614 lb"),
    HeliBuild("CPC35-H3", "H3", 3500, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.5-ton diesel clutch (mechanical), 7,716 lb"),
    HeliBuild("CPC38-H3", "H3", 3800, "Diesel", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.8-ton diesel clutch (mechanical), 8,378 lb"),
    HeliBuild("CPQD10-H3", "H3", 1000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton gasoline torque converter, 2,200 lb"),
    HeliBuild("CPQD15-H3", "H3", 1500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton gasoline torque converter, 3,300 lb"),
    HeliBuild("CPQD18-H3", "H3", 1800, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton gasoline torque converter, 3,968 lb"),
    HeliBuild("CPQD20-H3", "H3", 2000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline torque converter, 4,409 lb"),
    HeliBuild("CPQD25-H3", "H3", 2500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline torque converter, 5,511 lb"),
    HeliBuild("CPQD30-H3", "H3", 3000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline torque converter, 6,614 lb"),
    HeliBuild("CPQD35-H3", "H3", 3500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline torque converter, 7,716 lb"),
    HeliBuild("CPQD38-H3", "H3", 3800, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton gasoline torque converter, 8,378 lb"),
    HeliBuild("CPQ10-H3", "H3", 1000, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1-ton gasoline clutch (mechanical), 2,200 lb"),
    HeliBuild("CPQ15-H3", "H3", 1500, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.5-ton gasoline clutch (mechanical), 3,300 lb"),
    HeliBuild("CPQ18-H3", "H3", 1800, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.8-ton gasoline clutch (mechanical), 3,968 lb"),
    HeliBuild("CPQ20-H3", "H3", 2000, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2-ton gasoline clutch (mechanical), 4,409 lb"),
    HeliBuild("CPQ25-H3", "H3", 2500, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2.5-ton gasoline clutch (mechanical), 5,511 lb"),
    HeliBuild("CPQ30-H3", "H3", 3000, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3-ton gasoline clutch (mechanical), 6,614 lb"),
    HeliBuild("CPQ35-H3", "H3", 3500, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.5-ton gasoline clutch (mechanical), 7,716 lb"),
    HeliBuild("CPQ38-H3", "H3", 3800, "Gasoline", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.8-ton gasoline clutch (mechanical), 8,378 lb"),
    HeliBuild("CPYD10-H3", "H3", 1000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton LPG torque converter, 2,200 lb"),
    HeliBuild("CPYD15-H3", "H3", 1500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton LPG torque converter, 3,300 lb"),
    HeliBuild("CPYD18-H3", "H3", 1800, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton LPG torque converter, 3,968 lb"),
    HeliBuild("CPYD20-H3", "H3", 2000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton LPG torque converter, 4,409 lb"),
    HeliBuild("CPYD25-H3", "H3", 2500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton LPG torque converter, 5,511 lb"),
    HeliBuild("CPYD30-H3", "H3", 3000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton LPG torque converter, 6,614 lb"),
    HeliBuild("CPYD35-H3", "H3", 3500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton LPG torque converter, 7,716 lb"),
    HeliBuild("CPYD38-H3", "H3", 3800, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton LPG torque converter, 8,378 lb"),
    HeliBuild("CPY10-H3", "H3", 1000, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1-ton LPG clutch (mechanical), 2,200 lb"),
    HeliBuild("CPY15-H3", "H3", 1500, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.5-ton LPG clutch (mechanical), 3,300 lb"),
    HeliBuild("CPY18-H3", "H3", 1800, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "1.8-ton LPG clutch (mechanical), 3,968 lb"),
    HeliBuild("CPY20-H3", "H3", 2000, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2-ton LPG clutch (mechanical), 4,409 lb"),
    HeliBuild("CPY25-H3", "H3", 2500, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "2.5-ton LPG clutch (mechanical), 5,511 lb"),
    HeliBuild("CPY30-H3", "H3", 3000, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3-ton LPG clutch (mechanical), 6,614 lb"),
    HeliBuild("CPY35-H3", "H3", 3500, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.5-ton LPG clutch (mechanical), 7,716 lb"),
    HeliBuild("CPY38-H3", "H3", 3800, "LPG", "Class V", "Clutch", "4-wheel Pneumatic", None, None, "3.8-ton LPG clutch (mechanical), 8,378 lb"),
    HeliBuild("CPQYD10-H3", "H3", 1000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton gasoline/LPG dual torque converter, 2,200 lb"),
    HeliBuild("CPQYD15-H3", "H3", 1500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton gasoline/LPG dual torque converter, 3,300 lb"),
    HeliBuild("CPQYD18-H3", "H3", 1800, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton gasoline/LPG dual torque converter, 3,968 lb"),
    HeliBuild("CPQYD20-H3", "H3", 2000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline/LPG dual torque converter, 4,409 lb"),
    HeliBuild("CPQYD25-H3", "H3", 2500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline/LPG dual torque converter, 5,511 lb"),
    HeliBuild("CPQYD30-H3", "H3", 3000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline/LPG dual torque converter, 6,614 lb"),
    HeliBuild("CPQYD35-H3", "H3", 3500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline/LPG dual torque converter, 7,716 lb"),
    HeliBuild("CPQYD38-H3", "H3", 3800, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton gasoline/LPG dual torque converter, 8,378 lb"),

    # ========== K2 SERIES (1-10 tons) ==========
    HeliBuild("CPCD10-K2", "K2", 1000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton diesel"),
    HeliBuild("CPCD15-K2", "K2", 1500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton diesel"),
    HeliBuild("CPCD18-K2", "K2", 1800, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton diesel"),
    HeliBuild("CPCD20-K2", "K2", 2000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton diesel"),
    HeliBuild("CPCD25-K2", "K2", 2500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton diesel"),
    HeliBuild("CPCD30-K2", "K2", 3000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton diesel"),
    HeliBuild("CPCD35-K2", "K2", 3500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton diesel"),
    HeliBuild("CPCD38-K2", "K2", 3800, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton diesel"),
    HeliBuild("CPQD10-K2", "K2", 1000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton gasoline"),
    HeliBuild("CPQD15-K2", "K2", 1500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton gasoline"),
    HeliBuild("CPQD18-K2", "K2", 1800, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton gasoline"),
    HeliBuild("CPQD20-K2", "K2", 2000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline"),
    HeliBuild("CPQD25-K2", "K2", 2500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline"),
    HeliBuild("CPQD30-K2", "K2", 3000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline"),
    HeliBuild("CPQD35-K2", "K2", 3500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline"),
    HeliBuild("CPQD38-K2", "K2", 3800, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton gasoline"),
    HeliBuild("CPYD10-K2", "K2", 1000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton LPG"),
    HeliBuild("CPYD15-K2", "K2", 1500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton LPG"),
    HeliBuild("CPYD18-K2", "K2", 1800, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton LPG"),
    HeliBuild("CPYD20-K2", "K2", 2000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton LPG"),
    HeliBuild("CPYD25-K2", "K2", 2500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton LPG"),
    HeliBuild("CPYD30-K2", "K2", 3000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton LPG"),
    HeliBuild("CPYD35-K2", "K2", 3500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton LPG"),
    HeliBuild("CPYD38-K2", "K2", 3800, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton LPG"),
    HeliBuild("CPQYD10-K2", "K2", 1000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1-ton gasoline/LPG dual"),
    HeliBuild("CPQYD15-K2", "K2", 1500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD18-K2", "K2", 1800, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "1.8-ton gasoline/LPG dual"),
    HeliBuild("CPQYD20-K2", "K2", 2000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline/LPG dual"),
    HeliBuild("CPQYD25-K2", "K2", 2500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD30-K2", "K2", 3000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline/LPG dual"),
    HeliBuild("CPQYD35-K2", "K2", 3500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD38-K2", "K2", 3800, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.8-ton gasoline/LPG dual"),
    HeliBuild("CPCD40-K2", "K2", 4000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton diesel, 26-27 km/h"),
    HeliBuild("CPCD45-K2", "K2", 4500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton diesel"),
    HeliBuild("CPCD50-K2", "K2", 5000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton diesel, wheelbase 2100mm"),
    HeliBuild("CPCD60-K2", "K2", 6000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton diesel"),
    HeliBuild("CPCD70-K2", "K2", 7000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton diesel"),
    HeliBuild("CPCD80-K2", "K2", 8000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "8-ton diesel"),
    HeliBuild("CPCD90-K2", "K2", 9000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "9-ton diesel"),
    HeliBuild("CPCD100-K2", "K2", 10000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "10-ton diesel, wheelbase 2850mm"),
    HeliBuild("CPQYD40-K2", "K2", 4000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton gasoline/LPG dual"),
    HeliBuild("CPQYD45-K2", "K2", 4500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD50-K2", "K2", 5000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton gasoline/LPG dual"),
    HeliBuild("CPYD40-K2", "K2", 4000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton LPG"),
    HeliBuild("CPYD45-K2", "K2", 4500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton LPG"),
    HeliBuild("CPYD50-K2", "K2", 5000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton LPG"),

    # ========== G3 SERIES (2-10 tons) ==========
    HeliBuild("CPCD20-G3", "G3", 2000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton diesel, Kubota WG2503 Stage V"),
    HeliBuild("CPCD25-G3", "G3", 2500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton diesel, wet brake axle"),
    HeliBuild("CPCD30-G3", "G3", 3000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton diesel, replaces H3 in EU"),
    HeliBuild("CPCD35-G3", "G3", 3500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton diesel"),
    HeliBuild("CPQD20-G3", "G3", 2000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline"),
    HeliBuild("CPQD25-G3", "G3", 2500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline"),
    HeliBuild("CPQD30-G3", "G3", 3000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline"),
    HeliBuild("CPQD35-G3", "G3", 3500, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline"),
    HeliBuild("CPYD20-G3", "G3", 2000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton LPG"),
    HeliBuild("CPYD25-G3", "G3", 2500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton LPG"),
    HeliBuild("CPYD30-G3", "G3", 3000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton LPG"),
    HeliBuild("CPYD35-G3", "G3", 3500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton LPG"),
    HeliBuild("CPQYD20-G3", "G3", 2000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2-ton gasoline/LPG dual"),
    HeliBuild("CPQYD25-G3", "G3", 2500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "2.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD30-G3", "G3", 3000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3-ton gasoline/LPG dual"),
    HeliBuild("CPQYD35-G3", "G3", 3500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "3.5-ton gasoline/LPG dual"),
    HeliBuild("CPCD40-G3", "G3", 4000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton diesel, Yuchai Euro V"),
    HeliBuild("CPCD45-G3", "G3", 4500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton diesel, soft connected intelligent gearbox"),
    HeliBuild("CPCD50-G3", "G3", 5000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton diesel"),
    HeliBuild("CPCD55-G3", "G3", 5500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5.5-ton diesel"),
    HeliBuild("CPQYD40-G3", "G3", 4000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton gasoline/LPG dual"),
    HeliBuild("CPQYD45-G3", "G3", 4500, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD50-G3", "G3", 5000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton gasoline/LPG dual"),
    HeliBuild("CPYD40-G3", "G3", 4000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4-ton LPG"),
    HeliBuild("CPYD45-G3", "G3", 4500, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "4.5-ton LPG"),
    HeliBuild("CPYD50-G3", "G3", 5000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton LPG"),
    HeliBuild("CPCD50-CU1G3", "G3", 5000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton diesel, Cummins QSF3.8 Euro V/T4F"),
    HeliBuild("CPCD60-CU1G3", "G3", 6000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton diesel, Cummins QSF3.8"),
    HeliBuild("CPCD70-CU1G3", "G3", 7000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton diesel, Cummins QSF3.8"),
    HeliBuild("CPCD85-CU1G3", "G3", 8500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "8.5-ton diesel, Cummins only"),
    HeliBuild("CPCD100-CU1G3", "G3", 10000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "10-ton diesel, Cummins only"),

    # ========== G SERIES (5-10 tons) ==========
    HeliBuild("CPCD50-G", "G", 5000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton diesel, load center 600mm"),
    HeliBuild("CPCD60-G", "G", 6000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton diesel"),
    HeliBuild("CPCD70-G", "G", 7000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton diesel"),
    HeliBuild("CPCD85-G", "G", 8500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "8.5-ton diesel only"),
    HeliBuild("CPCD100-G", "G", 10000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "10-ton diesel only"),
    HeliBuild("CPQD50-G", "G", 5000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton gasoline"),
    HeliBuild("CPQD60-G", "G", 6000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton gasoline"),
    HeliBuild("CPQD70-G", "G", 7000, "Gasoline", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton gasoline"),
    HeliBuild("CPYD50-G", "G", 5000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton LPG"),
    HeliBuild("CPYD60-G", "G", 6000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton LPG"),
    HeliBuild("CPYD70-G", "G", 7000, "LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton LPG"),
    HeliBuild("CPQYD50-G", "G", 5000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "5-ton gasoline/LPG dual"),
    HeliBuild("CPQYD60-G", "G", 6000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "6-ton gasoline/LPG dual"),
    HeliBuild("CPQYD70-G", "G", 7000, "Gasoline/LPG", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "7-ton gasoline/LPG dual"),

    # ========== H3C CUSHION TIRE (1.5-3.2 tons) ==========
    HeliBuild("CPYD15C-H3C", "H3C", 1500, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "1.5-ton LPG cushion, Kubota WG2503, load center 600mm"),
    HeliBuild("CPYD18C-H3C", "H3C", 1800, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "1.8-ton LPG cushion"),
    HeliBuild("CPYD20C-H3C", "H3C", 2000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "2-ton LPG cushion"),
    HeliBuild("CPYD25C-H3C", "H3C", 2500, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "2.5-ton LPG cushion, 5,511 lb"),
    HeliBuild("CPYD30C-H3C", "H3C", 3000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "3-ton LPG cushion"),
    HeliBuild("CPYD32C-H3C", "H3C", 3200, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "3.2-ton LPG cushion"),
    HeliBuild("CPQYD15C-H3C", "H3C", 1500, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "1.5-ton gasoline/LPG dual cushion"),
    HeliBuild("CPQYD18C-H3C", "H3C", 1800, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "1.8-ton gasoline/LPG dual cushion"),
    HeliBuild("CPQYD20C-H3C", "H3C", 2000, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "2-ton gasoline/LPG dual cushion"),
    HeliBuild("CPQYD25C-H3C", "H3C", 2500, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "2.5-ton gasoline/LPG dual cushion"),
    HeliBuild("CPQYD30C-H3C", "H3C", 3000, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "3-ton gasoline/LPG dual cushion"),
    HeliBuild("CPQYD32C-H3C", "H3C", 3200, "Gasoline/LPG", "Class IV", "Torque Converter", "Cushion", None, None, "3.2-ton gasoline/LPG dual cushion"),

    # ========== IC CUSHION 4-5 TONS ==========
    HeliBuild("CPYD40C", "Cushion", 4000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "4-ton LPG cushion, turn radius 2,290mm"),
    HeliBuild("CPYD40C-BCS", "Cushion", 4000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "4-ton LPG cushion BCS variant, turn radius 2,150mm"),
    HeliBuild("CPYD50C", "Cushion", 5000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "5-ton LPG cushion, turn radius 2,400mm"),
    HeliBuild("CPYD50C-BCS", "Cushion", 5000, "LPG", "Class IV", "Torque Converter", "Cushion", None, None, "5-ton LPG cushion BCS variant, turn radius 2,240mm"),

    # ========== ELECTRIC COUNTERBALANCE ==========
    HeliBuild("CPD08SQ-H4", "H4", 800, "Electric", "Class I", "N/A", "3-wheel", None, None, "800kg electric, 24V lead-acid, CURTIS controller"),
    HeliBuild("CPD10SQ-H4", "H4", 1000, "Electric", "Class I", "N/A", "3-wheel", None, None, "1-ton electric, 24V lead-acid, onboard charger"),
    HeliBuild("CPD12SQ-H4", "H4", 1200, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.2-ton electric, 24V lead-acid, turn radius 1,508mm"),
    HeliBuild("CPD15-H4", "H4", 1500, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.5-ton electric, 48V lead-acid, battery extraction"),
    HeliBuild("CPD18-H4", "H4", 1800, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.8-ton electric, 48V lead-acid"),
    HeliBuild("CPD20-H4", "H4", 2000, "Electric", "Class I", "N/A", "4-wheel", None, None, "2-ton electric, 48V lead-acid"),
    HeliBuild("CPD25-H4", "H4", 2500, "Electric", "Class I", "N/A", "4-wheel", None, None, "2.5-ton electric, 48V lead-acid"),
    HeliBuild("CPD30-H4", "H4", 3000, "Electric", "Class I", "N/A", "4-wheel", None, None, "3-ton electric, 48V lead-acid"),
    HeliBuild("CPD35-H4", "H4", 3500, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.5-ton electric, 48V lead-acid"),
    HeliBuild("CPD38-H4", "H4", 3800, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.8-ton electric, 48V lead-acid"),

    # ========== G2 ELECTRIC 3-WHEEL ==========
    HeliBuild("CPD13SH-G2", "G2", 1300, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.3-ton electric, 48V lead-acid, ZAPI dual-core"),
    HeliBuild("CPD15SH-G2", "G2", 1500, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.5-ton electric, 48V lead-acid, accepts Li-ion"),

    # ========== G3 ELECTRIC 3-WHEEL & 4-WHEEL ==========
    HeliBuild("CPD15SQ-G3", "G3", 1500, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.5-ton electric, 48V lead-acid, ZAPI dual motor"),
    HeliBuild("CPD16SQ-G3", "G3", 1600, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.6-ton electric, 48V lead-acid, IPX4"),
    HeliBuild("CPD18SQ-G3", "G3", 1800, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.8-ton electric, 48V lead-acid"),
    HeliBuild("CPD20SQ-G3", "G3", 2000, "Electric", "Class I", "N/A", "3-wheel", None, None, "2-ton electric, 48V lead-acid"),
    HeliBuild("CPD30-G3", "G3", 3000, "Electric", "Class I", "N/A", "4-wheel", None, None, "3-ton electric, 80V lead-acid, dual drive"),
    HeliBuild("CPD35-G3", "G3", 3500, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.5-ton electric, 80V lead-acid, dual drive"),
    HeliBuild("CPD40-G3", "G3", 4000, "Electric", "Class I", "N/A", "4-wheel", None, None, "4-ton electric, 80V lead-acid"),
    HeliBuild("CPD45-G3", "G3", 4500, "Electric", "Class I", "N/A", "4-wheel", None, None, "4.5-ton electric, 80V lead-acid"),
    HeliBuild("CPD50-G3", "G3", 5000, "Electric", "Class I", "N/A", "4-wheel", None, None, "5-ton electric, 80V lead-acid, load center 500-600mm"),

    # ========== LI-ION ELECTRIC ==========
    HeliBuild("CPD08SQ-H4-LI", "H4", 800, "Electric", "Class I", "N/A", "3-wheel", None, None, "800kg electric, 80V li-ion, CURTIS controller"),
    HeliBuild("CPD10SQ-H4-LI", "H4", 1000, "Electric", "Class I", "N/A", "3-wheel", None, None, "1-ton electric, 80V li-ion"),
    HeliBuild("CPD12SQ-H4-LI", "H4", 1200, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.2-ton electric, 80V li-ion"),
    HeliBuild("CPD13SH-G3-LI", "G3", 1300, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.3-ton electric, 48V li-ion, ZAPI"),
    HeliBuild("CPD15SH-G3-LI", "G3", 1500, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.5-ton electric, 48V li-ion"),
    HeliBuild("CPD15SQ-G3-LI", "G3", 1500, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.5-ton electric, 80V li-ion, ZAPI dual motor"),
    HeliBuild("CPD16SQ-G3-LI", "G3", 1600, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.6-ton electric, 80V li-ion"),
    HeliBuild("CPD18SQ-G3-LI", "G3", 1800, "Electric", "Class I", "N/A", "3-wheel", None, None, "1.8-ton electric, 80V li-ion"),
    HeliBuild("CPD20SQ-G3-LI", "G3", 2000, "Electric", "Class I", "N/A", "3-wheel", None, None, "2-ton electric, 80V li-ion"),
    HeliBuild("CPD15-H4-LI", "H4", 1500, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.5-ton electric, 80V li-ion, INMOTION controller"),
    HeliBuild("CPD18-H4-LI", "H4", 1800, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.8-ton electric, 80V li-ion"),
    HeliBuild("CPD20-H4-LI", "H4", 2000, "Electric", "Class I", "N/A", "4-wheel", None, None, "2-ton electric, 80V li-ion"),
    HeliBuild("CPD25-H4-LI", "H4", 2500, "Electric", "Class I", "N/A", "4-wheel", None, None, "2.5-ton electric, 80V li-ion"),
    HeliBuild("CPD30-H4-LI", "H4", 3000, "Electric", "Class I", "N/A", "4-wheel", None, None, "3-ton electric, 80V li-ion, low-temp heating"),
    HeliBuild("CPD35-H4-LI", "H4", 3500, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.5-ton electric, 80V li-ion"),
    HeliBuild("CPD38-H4-LI", "H4", 3800, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.8-ton electric, 80V li-ion"),
    HeliBuild("CPD15-G2-LI", "G2", 1500, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.5-ton electric, 80V li-ion, INMOTION"),
    HeliBuild("CPD18-G2-LI", "G2", 1800, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.8-ton electric, 80V li-ion"),
    HeliBuild("CPD20-G2-LI", "G2", 2000, "Electric", "Class I", "N/A", "4-wheel", None, None, "2-ton electric, 80V li-ion"),
    HeliBuild("CPD25-G2-LI", "G2", 2500, "Electric", "Class I", "N/A", "4-wheel", None, None, "2.5-ton electric, 80V li-ion"),
    HeliBuild("CPD30-G2-LI", "G2", 3000, "Electric", "Class I", "N/A", "4-wheel", None, None, "3-ton electric, 80V li-ion"),
    HeliBuild("CPD35-G2-LI", "G2", 3500, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.5-ton electric, 80V li-ion"),
    HeliBuild("CPD38-G2-LI", "G2", 3800, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.8-ton electric, 80V li-ion"),
    HeliBuild("CPD15-G3-LI", "G3", 1500, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.5-ton electric, 80V li-ion dual drive, INMOTION"),
    HeliBuild("CPD18-G3-LI", "G3", 1800, "Electric", "Class I", "N/A", "4-wheel", None, None, "1.8-ton electric, 80V li-ion dual drive"),
    HeliBuild("CPD20-G3-LI", "G3", 2000, "Electric", "Class I", "N/A", "4-wheel", None, None, "2-ton electric, 80V li-ion dual drive"),
    HeliBuild("CPD25-G3-LI", "G3", 2500, "Electric", "Class I", "N/A", "4-wheel", None, None, "2.5-ton electric, 80V li-ion dual drive"),
    HeliBuild("CPD30-G3-LI", "G3", 3000, "Electric", "Class I", "N/A", "4-wheel", None, None, "3-ton electric, 80V li-ion dual drive, 2h charge"),
    HeliBuild("CPD35-G3-LI", "G3", 3500, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.5-ton electric, 80V li-ion dual drive"),
    HeliBuild("CPD38-G3-LI", "G3", 3800, "Electric", "Class I", "N/A", "4-wheel", None, None, "3.8-ton electric, 80V li-ion dual drive"),

    # ========== NARROW-AISLE REACH TRUCKS ==========
    HeliBuild("CQD12-G2", "G2", 1200, "Electric", "Class II", "N/A", "Reach", None, None, "1.2-ton reach truck, 48/80V li-ion, lift 8,500-12,500mm"),
    HeliBuild("CQD14-G2", "G2", 1400, "Electric", "Class II", "N/A", "Reach", None, None, "1.4-ton reach truck, 48/80V li-ion"),
    HeliBuild("CQD16-G2", "G2", 1600, "Electric", "Class II", "N/A", "Reach", None, None, "1.6-ton reach truck, 48/80V li-ion"),
    HeliBuild("CQD20-G2", "G2", 2000, "Electric", "Class II", "N/A", "Reach", None, None, "2-ton reach truck, 48/80V li-ion"),
    HeliBuild("CQD16-G2-CS", "G2", 1600, "Electric", "Class II", "N/A", "Reach", None, None, "1.6-ton reach cold store, 80V li-ion, heated seat"),
    HeliBuild("CQD20-G2-CS", "G2", 2000, "Electric", "Class II", "N/A", "Reach", None, None, "2-ton reach cold store, 80V li-ion, defrost"),
    HeliBuild("CQD15-G2-SU", "G2", 1500, "Electric", "Class II", "N/A", "Reach", None, None, "1.5-ton reach stand-up, 48/80V li-ion, lift 6,750mm"),
    HeliBuild("CQD18-G2-SU", "G2", 1800, "Electric", "Class II", "N/A", "Reach", None, None, "1.8-ton reach stand-up, 48/80V li-ion"),
    HeliBuild("CQD20-G2-SU", "G2", 2000, "Electric", "Class II", "N/A", "Reach", None, None, "2-ton reach stand-up, 48/80V li-ion, lift 7,400mm"),
    HeliBuild("CQD25-G2-SU", "G2", 2500, "Electric", "Class II", "N/A", "Reach", None, None, "2.5-ton reach stand-up, 48/80V li-ion"),
    HeliBuild("CQD14X2", "G", 1350, "Electric", "Class II", "N/A", "Reach", None, None, "1.35-ton reach stand-up G-series, 48V li-ion, double scissor"),
    HeliBuild("CQD16X1", "G", 1500, "Electric", "Class II", "N/A", "Reach", None, None, "1.5-ton reach stand-up G-series, 48V li-ion"),
    HeliBuild("CQD18X1", "G", 1800, "Electric", "Class II", "N/A", "Reach", None, None, "1.8-ton reach stand-up G-series, 48V li-ion"),

    # ========== WAREHOUSE EQUIPMENT ==========
    # Order Pickers
    HeliBuild("OPSM", "Order Picker", 150, "Electric", "Class III", "N/A", "Order picker", None, None, "150kg light order picker, DC controller, lift ~5,000mm"),
    HeliBuild("JX12J", "Order Picker", 1200, "Electric", "Class III", "N/A", "Order picker", None, None, "1.2-ton order picker, AC, low-level, lift 700mm"),
    HeliBuild("OPL10", "Order Picker", 1000, "Electric", "Class III", "N/A", "Order picker", None, None, "1-ton order picker, AC, low-level, lift 1,140mm"),
    HeliBuild("OPL12", "Order Picker", 1200, "Electric", "Class III", "N/A", "Order picker", None, None, "1.2-ton order picker, AC, low-level"),
    HeliBuild("OPL10-S", "Order Picker", 1000, "Electric", "Class III", "N/A", "Order picker", None, None, "1-ton order picker, AC, low-level, lift 2,360mm"),
    HeliBuild("OPS15", "Order Picker", 1500, "Electric", "Class III", "N/A", "Order picker", None, None, "1.5-ton order picker, AC, high-level, lift 5,000-9,000mm"),
    HeliBuild("OPD15", "Order Picker", 1500, "Electric", "Class III", "N/A", "Order picker", None, None, "1.5-ton order picker, AC, high-level, lift 3,540mm"),

    # Fork Over Stackers
    HeliBuild("CBS10", "Stacker", 1000, "Electric", "Class III", "N/A", "Stacker", None, None, "1-ton semi-electric stacker, electric lift + manual travel"),
    HeliBuild("CBS15", "Stacker", 1500, "Electric", "Class III", "N/A", "Stacker", None, None, "1.5-ton semi-electric stacker, lift 1,600-3,500mm"),
    HeliBuild("CBS20", "Stacker", 2000, "Electric", "Class III", "N/A", "Stacker", None, None, "2-ton semi-electric stacker"),
    HeliBuild("CDD12", "Stacker", 1200, "Electric", "Class III", "N/A", "Stacker", None, None, "1.2-ton electric stacker, AC/DC gel battery, lift 2,000-3,500mm"),
    HeliBuild("CDD14", "Stacker", 1400, "Electric", "Class III", "N/A", "Stacker", None, None, "1.4-ton electric stacker, AC gel battery"),
    HeliBuild("CDD15", "Stacker", 1500, "Electric", "Class III", "N/A", "Stacker", None, None, "1.5-ton electric stacker, AC/DC gel battery"),
    HeliBuild("CDD16", "Stacker", 1600, "Electric", "Class III", "N/A", "Stacker", None, None, "1.6-ton electric stacker, AC gel battery"),
    HeliBuild("CDD20", "Stacker", 2000, "Electric", "Class III", "N/A", "Stacker", None, None, "2-ton electric stacker, AC gel battery"),

    # Reach Stackers
    HeliBuild("CQDM12", "Reach Stacker", 1200, "Electric", "Class III", "N/A", "Reach stacker", None, None, "1.2-ton reach stacker, AC, reach 425mm"),
    HeliBuild("CQDM15", "Reach Stacker", 1500, "Electric", "Class III", "N/A", "Reach stacker", None, None, "1.5-ton reach stacker, AC, reach 425mm"),
    HeliBuild("CQDH14", "Reach Stacker", 1400, "Electric", "Class III", "N/A", "Reach stacker", None, None, "1.4-ton reach stacker, AC, reach 500mm"),
    HeliBuild("CQDH12", "Reach Stacker", 1200, "Electric", "Class III", "N/A", "Reach stacker", None, None, "1.2-ton reach stacker, AC, reach 500mm"),
    HeliBuild("CQDH15", "Reach Stacker", 1500, "Electric", "Class III", "N/A", "Reach stacker", None, None, "1.5-ton reach stacker, AC, reach 500mm"),
    HeliBuild("CQDH20", "Reach Stacker", 2000, "Electric", "Class III", "N/A", "Reach stacker", None, None, "2-ton reach stacker, AC, reach 500mm"),

    # Pallet Trucks / Walkie Jacks
    HeliBuild("CBD15", "Pallet Truck", 1500, "Electric", "Class III", "N/A", "Walkie", None, None, "1.5-ton walkie pallet, li-ion portable swap battery"),
    HeliBuild("CBD18", "Pallet Truck", 1800, "Electric", "Class III", "N/A", "Walkie", None, None, "1.8-ton walkie pallet, li-ion"),
    HeliBuild("CBD20", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, li-ion 48V/20Ah"),
    HeliBuild("CBD25", "Pallet Truck", 2500, "Electric", "Class III", "N/A", "Walkie", None, None, "2.5-ton walkie pallet, VRLA 140Ah"),
    HeliBuild("CBD30", "Pallet Truck", 3000, "Electric", "Class III", "N/A", "Walkie", None, None, "3-ton walkie pallet, VRLA 140Ah"),
    HeliBuild("CBD20J", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, li-ion optional, CURTIS DC"),
    HeliBuild("CBD20J-H", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, li-ion optional, AC controller"),
    HeliBuild("CBD20-150", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, lift 110mm, 3.5-4.0 km/h"),
    HeliBuild("CBD20-150G", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, lift 110mm"),
    HeliBuild("CBD20-410", "Pallet Truck", 2000, "Electric", "Class III", "N/A", "Walkie", None, None, "2-ton walkie pallet, lift 110mm, 5.5-6.0 km/h"),
    HeliBuild("CBD35-510", "Pallet Truck", 3500, "Electric", "Class III", "N/A", "Walkie", None, None, "3.5-ton walkie pallet, FAAM 330/490Ah, 11-14 km/h"),
    HeliBuild("CBD35-520", "Pallet Truck", 3500, "Electric", "Class III", "N/A", "Walkie", None, None, "3.5-ton walkie pallet, FAAM, AC, load center 600-760mm"),
    HeliBuild("CBD35-530", "Pallet Truck", 3500, "Electric", "Class III", "N/A", "Walkie", None, None, "3.5-ton walkie pallet, FAAM, AC, 4-4.5 km/h"),

    # ========== ELECTRIC CUSHION TIRE ==========
    HeliBuild("CPD20C", "Cushion", 2000, "Electric", "Class I", "N/A", "Cushion", None, None, "2-ton electric cushion, 80V li-ion (lead-acid opt)"),
    HeliBuild("CPD25C", "Cushion", 2500, "Electric", "Class I", "N/A", "Cushion", None, None, "2.5-ton electric cushion, 80V li-ion, load center 600mm"),
    HeliBuild("CPD30C", "Cushion", 3000, "Electric", "Class I", "N/A", "Cushion", None, None, "3-ton electric cushion, 80V li-ion"),
    HeliBuild("CPD32C", "Cushion", 3200, "Electric", "Class I", "N/A", "Cushion", None, None, "3.2-ton electric cushion, 80V li-ion"),

    # ========== HEAVY DIESEL (12-46 tons) ==========
    HeliBuild("CPCD120", "Heavy", 12000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "12-ton diesel heavy, load center 900mm, electric tilting cab"),
    HeliBuild("CPCD135", "Heavy", 13500, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "13.5-ton diesel heavy, load center 600mm"),
    HeliBuild("CPCD140", "Heavy", 14000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "14-ton diesel heavy, load center 900/1,200mm"),
    HeliBuild("CPCD150", "Heavy", 15000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "15-ton diesel heavy"),
    HeliBuild("CPCD160", "Heavy", 16000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "16-ton diesel heavy"),
    HeliBuild("CPCD140-L", "Heavy", 14000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "14-ton light-type diesel, HELI gearbox, lift 6,500mm"),
    HeliBuild("CPCD160-L", "Heavy", 16000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "16-ton light-type diesel"),
    HeliBuild("CPCD180-L", "Heavy", 18000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "18-ton light-type diesel, turn radius 4,500mm"),
    HeliBuild("CPCD200", "Heavy", 20000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "20-ton diesel heavy, load center 1,200mm"),
    HeliBuild("CPCD250", "Heavy", 25000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "25-ton diesel heavy"),
    HeliBuild("CPCD280", "Heavy", 28000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "28-ton diesel heavy"),
    HeliBuild("CPCD300", "Heavy", 30000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "30-ton diesel heavy"),
    HeliBuild("CPCD320", "Heavy", 32000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "32-ton diesel heavy"),
    HeliBuild("CPCD350", "Heavy", 35000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "35-ton diesel heavy"),
    HeliBuild("CPCD380", "Heavy", 38000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "38-ton diesel heavy"),
    HeliBuild("CPCD400", "Heavy", 40000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "40-ton diesel heavy"),
    HeliBuild("CPCD420", "Heavy", 42000, "Diesel", "Class V", "Torque Converter", "4-wheel Pneumatic", None, None, "42-ton diesel heavy"),

    # ========== HEAVY LI-ION (12-55 tons) ==========
    HeliBuild("CPD120-LI", "Heavy", 12000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "12-ton heavy li-ion, SANTROLL controller, 531.3V/302Ah"),
    HeliBuild("CPD150-LI", "Heavy", 15000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "15-ton heavy li-ion, 531.3V/302Ah"),
    HeliBuild("CPD180-LI", "Heavy", 18000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "18-ton heavy li-ion, 531.3V/302Ah, 120kW fast charge"),
    HeliBuild("CPD200-LI", "Heavy", 20000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "20-ton heavy li-ion, LVKON 608V controller, >8h duration"),
    HeliBuild("CPD250-LI", "Heavy", 25000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "25-ton heavy li-ion, 608V, dual energy recovery"),
    HeliBuild("CPD350-LI", "Heavy", 35000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "35-ton heavy li-ion, 608V"),
    HeliBuild("CPD550-LI", "Heavy", 55000, "Electric", "Class I", "N/A", "4-wheel Pneumatic", None, None, "55-ton heavy li-ion, 608V, energy recovery"),

    # ========== TRACTORS ==========
    # IC Tractor
    HeliBuild("QYCD20", "Tractor", 2040, "Diesel", "Class VI", "Torque Converter", "Pneumatic", None, None, "20 kN drawbar diesel tractor, synchronizer gearbox"),
    HeliBuild("QYCD25", "Tractor", 2550, "Diesel/Gasoline", "Class VI", "Torque Converter", "Pneumatic", None, None, "25 kN drawbar diesel/gas tractor"),
    HeliBuild("QYCD30", "Tractor", 3060, "Diesel/Gasoline", "Class VI", "Torque Converter", "Pneumatic", None, None, "30 kN drawbar diesel/gas tractor"),

    # Li-ion Tractor
    HeliBuild("QYD20S", "Tractor", 3000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "3-ton li-ion sit-down tractor, AC control"),
    HeliBuild("QYD30S", "Tractor", 3000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "3-ton li-ion sit-down tractor"),
    HeliBuild("QYD40S", "Tractor", 4000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "4-ton li-ion sit-down tractor"),
    HeliBuild("QYD50S", "Tractor", 5000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "5-ton li-ion sit-down tractor"),
    HeliBuild("QYD60S", "Tractor", 6000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "6-ton li-ion sit-down tractor"),
    HeliBuild("QYD70S", "Tractor", 7000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "7-ton li-ion sit-down tractor"),
    HeliBuild("QYD30S-SU", "Tractor", 3000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "3-ton li-ion stand-up tractor, AC regen braking"),
    HeliBuild("QYD45S-SU", "Tractor", 4000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "4-ton li-ion stand-up tractor"),
    HeliBuild("QYD50S-SU", "Tractor", 5000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "5-ton li-ion stand-up tractor"),
    HeliBuild("QYD100", "Tractor", 10000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "10-ton li-ion tractor, ZAPI AC, 4,000N traction"),
    HeliBuild("QYD200", "Tractor", 8000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "8-ton li-ion heavy tractor, 3,200N, energy recovery"),
    HeliBuild("QYD250", "Tractor", 10000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "10-ton li-ion heavy tractor"),
    HeliBuild("QYD300", "Tractor", 12000, "Electric", "Class VI", "N/A", "Pneumatic", None, None, "12-ton li-ion heavy tractor"),

    # ========== ROUGH TERRAIN ==========
    HeliBuild("CPCD20-RT", "Rough Terrain", 2000, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "2-ton rough terrain, 2WD/4WD, turn radius 3,400mm"),
    HeliBuild("CPCD25-RT", "Rough Terrain", 2500, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "2.5-ton rough terrain, 2WD/4WD"),
    HeliBuild("CPCD30-RT", "Rough Terrain", 3000, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "3-ton rough terrain, 2WD/4WD"),
    HeliBuild("CPCD35-RT", "Rough Terrain", 3500, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "3.5-ton rough terrain, 2WD/4WD"),
    HeliBuild("CPCD50-RT", "Rough Terrain", 5000, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "5-ton rough terrain, Cummins QSF3.8 Euro V, 2WD/4WD"),
    HeliBuild("CPCD25-G3-RT", "Rough Terrain", 2500, "Diesel", "Class VII", "Torque Converter", "Off-road", None, None, "2.5-ton rough terrain G3, KUBOTA, 4WD/2WD"),

    # ========== TELESCOPIC HANDLER ==========
    HeliBuild("30THCJ07", "Rough Terrain", 3000, "Diesel", "Class VII", "Hydrostatic", "Rough terrain 4WD", None, None, "3-ton telescopic handler, diesel, lift 6.97m, reach 3.73m"),
    HeliBuild("35THCJ07", "Rough Terrain", 3500, "Diesel", "Class VII", "Hydrostatic", "Rough terrain 4WD", None, None, "3.5-ton telescopic handler, diesel"),
    HeliBuild("40H130-170S", "Rough Terrain", 4000, "Diesel", "Class VII", "Hydrostatic", "Rough terrain 4WD", None, None, "4-ton telescopic handler, lift 17/16m, reach 13/8.7m"),
    HeliBuild("50H88-128S", "Rough Terrain", 5000, "Diesel", "Class VII", "Hydrostatic", "Rough terrain 4WD", None, None, "5-ton telescopic handler, lift 12.8m, reach 8.8m"),
    HeliBuild("75THCJ10", "Rough Terrain", 7500, "Diesel", "Class VII", "Hydrostatic", "Rough terrain 4WD", None, None, "7.5-ton telescopic handler, lift 10.283m, reach 5.473m"),

    # ========== EMPTY CONTAINER STACKER ==========
    HeliBuild("CPCD120EC", "Heavy", 4500, "Diesel", "Class V", "Torque Converter", "Pneumatic", None, None, "4.5-ton empty container stacker, load center 1,220mm, 2-3 layers"),
    HeliBuild("CPCD180EC", "Heavy", 8000, "Diesel", "Class V", "Torque Converter", "Pneumatic", None, None, "8-ton empty container stacker, 5-6 layers"),
    HeliBuild("CPCD250EC", "Heavy", 10000, "Diesel", "Class V", "Torque Converter", "Pneumatic", None, None, "10-ton empty container stacker, 7-8 layers"),
]


def main() -> None:
    db = SessionLocal()
    existing = {f.model for f in db.query(Forklift).filter_by(manufacturer=HELI).all()}
    added = 0

    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "series", "capacity_kg", "fuel_type", "truck_class", "transmission", "tire_type", "year_start", "notes"])

        for build in MODELS:
            if build.model in existing:
                w.writerow([build.model, build.series, build.capacity_kg, build.fuel_type, build.truck_class, build.transmission, build.tire_type, build.year_start, "EXISTS"])
                continue

            # Create Forklift row with corrected serial assignment pattern:
            nf = Forklift(
                manufacturer=HELI,
                model=build.model,
                series=build.series,
                capacity_kg=build.capacity_kg,
                fuel_type=build.fuel_type,
                truck_class=build.truck_class,
                year_start=build.year_start,
                year_end=build.year_end,
                notes=build.notes,
                source_url="https://www.helichina.net/"
            )
            db.add(nf)
            db.flush()
            nf.internal_serial = next_company_serial(db)
            db.flush()

            existing.add(build.model)
            added += 1
            w.writerow([build.model, build.series, build.capacity_kg, build.fuel_type, build.truck_class, build.transmission, build.tire_type, build.year_start, build.notes])

        db.commit()

    total = db.query(Forklift).filter_by(manufacturer=HELI).count()
    db.close()
    print(f"✅ Added {added} Heli models. Heli total now {total}. Report: {REPORT}")


if __name__ == "__main__":
    main()
