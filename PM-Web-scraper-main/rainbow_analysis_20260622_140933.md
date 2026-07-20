# Unscheduled Work Order Analysis: rainbow

_Generated 2026-06-22 14:09 using gemini-2.5-flash from 41 work orders_

# Reliability Analysis Report: TLF Intellistore (Rainbow Stacking System) - TLF211

## 1. Executive Summary
The "Rainbow" stacking system exhibits low reliability characterized by frequent mechanical deformations and sensor failures. While the machine experiences a variety of faults, the most critical issues are systemic "crashes" and alignment drifts that lead to significant downtime and expensive component replacements. The system is heavily impacted by external physical damage (material handling collisions) and sensitivity to power fluctuations.

## 2. Recurring Failure Modes

### Mechanical Deformation & Bracket Failure
*   **Occurrence:** ~10 instances
*   **Example WOs:** 2321, 3424, 6874, 7321, 7950, 12493, 12513, 12657, 14963
*   **Likely Root Cause:** Structural weakness in bracketry combined with operational "crashes" (often overnight) and general wear and tear. The repeated bending and cracking of suction cup holders and center beams suggest these parts are under-specified for the actual operational stresses.

### Referencing & Axis Synchronization Errors
*   **Occurrence:** ~8 instances
*   **Example WOs:** 2412, 8068, 9349, 9668, 10159, 14421, 14794
*   **Likely Root Cause:** Misalignment of the main beam, contaminated reflectors (black smears), or electronic synchronization loss. Several instances were triggered by power outages, suggesting the machine loses its home position or experiences logic faults during power cycles.

### Sensor & Cable Faults
*   **Occurrence:** ~7 instances
*   **Example WOs:** 2109, 7676, 13239, 13818, 14153, 14959, 16138
*   **Likely Root Cause:** Physical impact/vibration causing cables to detach or sensors to shift. The IE5338 sensor is a recurring failure point, often damaged during "crashes."

### External Physical Damage
*   **Occurrence:** 3 instances
*   **Example WOs:** 9253, 15319, 18930
*   **Likely Root Cause:** Collisions with material handlers/forklifts affecting safety light barriers and fencing.

## 3. Trends Over Time
The machine has not shown a definitive trend toward improvement; rather, it oscillates between periods of stability and clusters of failures. 
*   **Initial Major Failure:** A massive overhaul occurred in late 2022 (WO 2412), involving extensive part replacements and high cost.
*   **Crash Clustering:** There is a notable cluster of "crash-related" failures in 2024 (WOs 12513, 12657, 12798), where one event led to a cascade of bent brackets and beams that required staggered replacements.
*   **Stability:** The frequency of "Prod Stopped" events remains consistent across the timeline, indicating that the root cause of mechanical instability is not being resolved by simple part replacement.

## 4. Cost & Downtime Impact
*   **Total Material Cost:** \$34,637.77
*   **Total Downtime:** 187.6 Hours
*   **Critical Event:** WO 2412 was the single most impactful event, costing **\$23,143.87** in materials and accounting for **160 hours** of downtime (approximately 85% of the total downtime for the entire history).
*   **Recurring Cost:** Brackets and fixing devices are a constant drain, with individual replacements ranging from \$1,200 to \$2,000 (e.g., WOs 12493, 12513).

## 5. Root-Cause Hypotheses
1.  **Structural Insufficiency:** The "fixing devices" and brackets are designed for precision but lack the robustness to survive minor operational misalignments, leading to a "bend-break-replace" cycle.
2.  **Environmental Vulnerability:** The machine is highly sensitive to power quality. Multiple "Prod Stopped" events (WOs 8045, 9668, 19844) were caused by power outages or breaker trips, indicating a lack of adequate surge protection or robust recovery logic.
3.  **Operational Misalignment:** The frequent "overnight crashes" suggest that when the machine runs unattended, slight shifts in material position result in high-impact collisions that the system cannot detect or mitigate in time.

## 6. Recommended Actions

### High Priority (Immediate)
*   **Hardware Reinforcement:** Evaluate the "fixing device" (Part# 25-206-66120) and "bracket holder" (Part# 30-246-76581). Consider fabricating reinforced versions or adding physical stop-blocks to prevent the beams from bending during a crash.
*   **Critical Spares Kit:** Maintain an on-site stock of **Sensor IE5338** and the **Fixing Device brackets**, as these are the most frequent failure points.

### Medium Priority (Preventive)
*   **Enhanced PM Schedule:** Implement a bi-monthly "Laser & Reflector Cleaning" and "Beam Leveling Check" as recommended by the tech in WO 14794.
*   **Physical Protection:** Install heavy-duty bollards or guardrails around the safety sensor polls and light barriers to prevent further material handler collisions (WOs 9253, 18930).

### Long Term (Reliability Engineering)
*   **Power Conditioning:** Install a dedicated UPS or surge protector for the Rainbow controller to eliminate the "loss of reference" and "breaker trip" issues following power fluctuations.
*   **Crash Logic Review:** Work with Stiles tech support to determine if sensor sensitivity or "slow-down" zones can be implemented to prevent the high-impact crashes occurring overnight.