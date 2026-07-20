# Unscheduled Work Order Analysis: rainbow

_Generated 2026-06-19 10:26 using gemini-2.5-flash_

This report analyzes the unscheduled work-order history for the "rainbow" factory equipment to identify recurring failure modes, trends, and recommend actions to improve reliability.

### 1. Executive Summary

The "rainbow" stacking system experiences frequent breakdowns, leading to significant cumulative downtime and material costs. A substantial portion of issues stem from physical impacts and resultant mechanical damage or misalignment, often compounded by sensor failures and X-axis movement problems. One catastrophic X-axis failure accounted for over two-thirds of the total material cost and 85% of downtime, but numerous smaller, impact-related failures are also a consistent drain on resources. Addressing operational practices, enhancing preventive maintenance, and strategic spare parts stocking are crucial for improving this asset's reliability.

### 2. Recurring Failure Modes

The work order history reveals several key recurring failure modes:

*   **Mechanical Damage & Misalignment (often from impact/wear):** This is the most prevalent failure mode, encompassing bent, broken, or misaligned structural components. These issues are frequently attributed to "hits," "bumps," or "crashes" during operation, as well as general wear and tear leading to loosening or fatigue.
    *   **Occurrences:** 21 work orders.
    *   **Example WO Numbers:** WO 2321 (bent arm holder), WO 3424 (broken holder arm), WO 6874 (bent suction pipe brackets/beams), WO 12513 (cracked center beams from crashes), WO 10219 (bent controller column), WO 16138 (bent bracket).
    *   **Likely Root Cause:** Operator error (e.g., collisions during material handling, incorrect operational sequences), inadequate structural design/materials for operational stresses, gradual loosening of fasteners due to vibration, and fatigue from repeated minor impacts.

*   **Sensor & Control System Failures:** Problems with various sensors, their associated cables, or the immediate control signals they provide. These failures can lead to issues with measurement, suction, or referencing.
    *   **Occurrences:** 8 work orders.
    *   **Example WO Numbers:** WO 2109 (width measurement sensor/encoder not retracting), WO 7676 (suction sensor IE5338 replaced), WO 13239 (damaged sensor IE5338 from sheet crash), WO 14959 (damaged vacuum sensor IE5338 from overnight crash).
    *   **Likely Root Cause:** Direct impact damage to sensors (often linked to the mechanical damage category), wear-out, environmental factors like dust or smears affecting optical sensors/reflectors, and cable fatigue.

*   **X-axis & Referencing Drive Issues:** Failures related to the machine's primary horizontal movement, including synchronization problems, drive tire issues, and the inability to establish a reference point.
    *   **Occurrences:** 8 work orders.
    *   **Example WO Numbers:** WO 2412 (a massive X-axis failure involving synchronization and many component replacements), WO 4058, 4059, 5829 (recurring drive tire movement checks), WO 10159 (failure to reference due to X-axis not in sync), WO 14794 (Drive Unit Frequency Converter trip, X-axis deactivated).
    *   **Likely Root Cause:** Underlying mechanical misalignment, worn drive components (wheels, bearings), sensor feedback issues, and electrical/control system faults (including power disturbances).

*   **Power Supply & Electrical Interruption:** Direct problems with the machine's power supply or its recovery from external power outages.
    *   **Occurrences:** 3 work orders.
    *   **Example WO Numbers:** WO 8045 (crane jerking after overnight power outage), WO 9668 (machine won't reference after power outage), WO 19844 (machine not turning on, breaker reset).
    *   **Likely Root Cause:** Instability in the external power grid, sensitivity of the machine's control systems to power fluctuations, and minor internal electrical trips.

*   **Pneumatic System Failures:** Issues related to the machine's compressed air system.
    *   **Occurrences:** 2 work orders.
    *   **Example WO Numbers:** WO 3037 (air regulator leaking), WO 12876 (leaking cylinder).
    *   **Likely Root Cause:** Component wear and tear, and degradation of seals within the pneumatic system.

### 3. Trends Over Time

The data spans nearly four years (August 2022 to June 2026). While the total number of work orders per year remains somewhat consistent, there are clear trends in the **nature and impact of failures**:

*   **Early Catastrophic Failure:** WO 2412, starting in September 2022 and lasting until May 2023, represents a significant X-axis system failure. This event alone accounts for the majority of the total downtime and material cost, indicating a severe initial reliability issue or a major installation/commissioning problem early in the machine's life.
*   **Escalation of Impact-Related Damage:** There is a noticeable increase in explicitly mentioned "crashes," "hits," and "bumps" causing mechanical damage, particularly from mid-2024 onwards. Examples include:
    *   WO 12513 (July 2024): Mentions the machine "crashed twice when left running overnight not long ago."
    *   WO 12657 (August 2024): Notes a component was bent "from the crash it had weeks ago."
    *   WO 12798 (August 2024): "Night before system dropped a sheet overnight breaking the sheet."
    *   WO 13239 (September 2024): Operator advised "sheet crashed into the back stops."
    *   WO 13818 (November 2024): Refers to "all the Hits the machine takes like last week when the sheet fell off."
    *   WO 14153 (December 2024): "Lead did advise the machine crashed yesterday."
    *   WO 14959 (March 2025): "Looks like machine crashed overnight."
    This clustering strongly suggests a worsening trend in operational practices or control system stability, leading to repeated physical damage.
*   **Recurring Power-Related Issues:** While few in number, power outages consistently trigger referencing problems (WO 8045, 9668) or prevent machine startup (WO 19844), indicating sensitivity to electrical supply stability.

### 4. Cost & Downtime Impact

The "rainbow" equipment has incurred substantial costs and downtime:

*   **Total Material Cost:** $34,637.77
*   **Total Labor Time:** 356.1 hours
*   **Total Downtime Hours:** 187.6 hours

**Most Impactful Events:**

*   **WO 2412 (X-axis Failure):** This single work order accounts for an overwhelming majority of the impact:
    *   **Material Cost:** $23,143.87 (66.8% of total)
    *   **Downtime:** 160 hours (85.3% of total)
    This was a multi-month repair involving numerous part replacements and extensive labor.
*   **Other High-Cost Material Events (excluding WO 2412):**
    *   WO 12513 (cracked center beams from crashes): $2,000
    *   WO 2321 (bent arm holder): $1,410
    *   WO 12657 (bent fixing device from crash): $1,380
    *   WO 7321 (bent/cracked middle bracket): $1,367
    *   WO 14963 (bent center bracket): $1,367
    *   WO 12493 (bent/cracked suction cup bracket): $1,320
    *   WO 3424 (broken holder arm): $1,267
These indicate a pattern of significant material expenditure on mechanical components that are frequently damaged.

*   **Other High-Downtime Events (excluding WO 2412):**
    *   WO 14421 (won't reference, scissor lift not straight): 7 hours
    *   WO 14794 (Drive Unit Frequency Converter Trip): 4 hours
    *   WO 14153 (cross rail sensor, stuck): 2.5 hours
    The remaining downtime is distributed across many smaller events, typically 0.5-2 hours per incident, emphasizing the cumulative effect of frequent, shorter breakdowns.

### 5. Root-Cause Hypotheses

Based on the recurring failure modes and trends, several root-cause hypotheses emerge:

1.  **Systemic Operational Issues / Human Factor:** The high frequency of "crashes," "hits," and "bumps" strongly suggests that operational procedures, operator training, or material handling practices are inadequate. Instances like sheets falling off due to "air compressor was not on but dryer was" (WO 13818) point to a lack of proper interlocks or procedural adherence. "Machine crashed twice when left running overnight" (WO 12513) further supports a systemic issue with unattended operation or error handling.
2.  **Vibration and Fatigue:** The repeated mention of loose hardware, broken bolts, and components "bending upwards over time" (WO 6874) indicates that operational vibration and cyclical stresses are causing fatigue and loosening of fasteners, leading to misalignment and component failure.
3.  **Inadequate Component Robustness/Design:** The frequent bending and cracking of "arm holders," "brackets," and "beams" (e.g., WO 2321, 12513, 14963) suggests these parts may be under-engineered for the operational environment or susceptible to damage from even minor impacts.
4.  **Environmental Sensitivity:** The direct correlation between power outages and referencing issues points to the machine's sensitivity to power quality or its inability to recover gracefully from power interruptions. Dust/smears affecting reflectors (WO 10159) also highlight environmental factors.
5.  **Lack of Proactive Maintenance & Monitoring:** Many issues appear to escalate from minor misalignments or loose parts to more significant failures, suggesting that routine checks might not be catching these issues early enough. The continuous "drive tire movement" checks (WO 4058, 4059, 5829) without definitive resolution further support this.

### 6. Recommended Actions

To improve the reliability of the "rainbow" equipment, a multi-faceted approach addressing the identified root causes is necessary.

**Prioritized Recommendations:**

1.  **Operational Excellence & Training Program (High Priority, Immediate Impact):**
    *   **Action:** Conduct a thorough review of all operational procedures, especially for material loading/unloading, setup, and unattended operation.
    *   **Action:** Implement mandatory, recurrent operator training focusing on collision avoidance, understanding machine alarms, proper startup/shutdown, and troubleshooting initial faults.
    *   **Action:** Establish clear protocols for what to do when a "crash" or "hit" occurs, including immediate inspection and reporting.
    *   *Rationale:* Directly addresses the primary root cause of mechanical damage and misalignment, which is largely attributed to operational impacts. Reduces catastrophic failures and minor damages.

2.  **Enhanced Preventive Maintenance (PM) Program (High Priority, Sustained Impact):**
    *   **Action:** Develop a detailed PM checklist focusing on known failure points:
        *   **Structural Integrity:** Regular inspection (e.g., weekly/bi-weekly) of all brackets, arms, beams, and covers for bending, cracking, or misalignment. Specifically target suction cup mounts, sensor brackets, and drive mechanisms.
        *   **Fastener Torque Checks:** Periodically check and re-torque critical fasteners on all moving components, especially around the X-axis drive, suction assemblies, and sensor mounts.
        *   **Sensor & Reflector Cleaning:** Implement a regular schedule (e.g., monthly) for cleaning X-axis reflectors (WO 10159) and other optical sensors.
        *   **Drive Train Inspection:** Monitor drive tire movement (WO 4058, 4059, 5829) and inspect associated bearings/shafts for wear or play.
        *   **Pneumatic System Checks:** Routine inspection for air leaks (WO 3037) and cylinder condition (WO 12876).
    *   *Rationale:* Proactively identifies and corrects issues before they escalate into breakdowns, reducing the frequency of mechanical and sensor failures.

3.  **Strategic Spare Parts Inventory (High Priority, Immediate Impact):**
    *   **Action:** Ensure critical and frequently replaced components are stocked on-site or with rapid procurement channels. These include:
        *   Bent/broken brackets/arms (e.g., Part # 30-246-76581, 25-206-66120, 25-406-66110 for WO 2321, 3424, 7321, 12493, 12513, 12657, 14963).
        *   Frequently failing sensors (e.g., IE5338 for WO 7676, 13239, 14959).
        *   Air regulators and common cylinders (Part # 25-035-12892 for WO 3037, 12876).
        *   Encoder/width measurement sensor (WO 2109).
        *   Miscellaneous structural fasteners (WO 7931, 10891, 13929).
    *   *Rationale:* Reduces downtime significantly by enabling quicker repairs for common failures.

4.  **Power Quality Assessment & Mitigation (Medium Priority):**
    *   **Action:** Investigate the facility's power quality to the "rainbow" machine.
    *   **Action:** Consider installing a dedicated Uninterruptible Power Supply (UPS) or power conditioning unit for the machine's control system to buffer against voltage fluctuations and short outages (WO 8045, 9668, 19844).
    *   *Rationale:* Mitigates referencing and startup issues directly linked to power instability.

5.  **Root Cause Analysis for Major Crashes (Medium Priority, Long-term Impact):**
    *   **Action:** For any future significant "crash" event, conduct a formal Root Cause Analysis (RCA) using a structured methodology (e.g., 5 Whys, Fishbone Diagram). Focus on identifying the exact trigger, control system status, operator actions, and environmental conditions.
    *   **Action:** Explore options for advanced diagnostics or data logging from the machine's control system to better understand pre-crash conditions and machine behavior (WO 12513, 14959).
    *   *Rationale:* Moves beyond simply fixing symptoms to identifying and eliminating fundamental causes of severe, high-cost failures.

6.  **Review of Vulnerable Component Design (Long-Term Priority):**
    *   **Action:** Engage with the OEM (Stiles) regarding the recurring failures of specific mechanical components (e.g., arm holders, suction pipe brackets).
    *   **Action:** Explore potential upgrades to more robust materials, improved mounting designs, or protective enclosures for components frequently subject to bending or cracking.
    *   *Rationale:* Addresses potential inherent design weaknesses that contribute to frequent mechanical damage.