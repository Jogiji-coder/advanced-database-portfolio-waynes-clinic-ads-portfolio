// =====================================================================
// Wayne's Clinic Baltimore — Portfolio 4 · Document model (MongoDB 8)
// Script 03 · The three core operations + analytics, with explain() evidence
// Run:  mongosh --quiet "mongodb://127.0.0.1:27017/waynes_clinic?replicaSet=rs0" 03_core_operations.js
// =====================================================================
db = db.getSiblingDB("waynes_clinic");
const line = (t) => print("\n=== " + t + " " + "=".repeat(Math.max(0, 66 - t.length)));
const show = (x) => printjson(x);

// Baltimore-local weekday and HH:MM for a Date
function localParts(d) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hourCycle: "h23"
  }).formatToParts(d).map((x) => [x.type, x.value]));
  return { weekday: { Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6, Sun: 7 }[p.weekday], hhmm: p.hour + ":" + p.minute };
}
// Next Wednesday at a Baltimore wall-clock time (EST/EDT handled by Intl)
function nextWednesdayAt(hhmm) {
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + ((10 - now.getUTCDay()) % 7 || 7)));
  const [h, m] = hhmm.split(":").map(Number);
  for (const off of [4, 5]) {                  // EDT = UTC-4, EST = UTC-5
    const c = new Date(d.getTime() + (h + off) * 3600e3 + m * 60e3);
    if (localParts(c).hhmm === hhmm) return c;
  }
}

if (!db.counters.findOne({ _id: "encounter" })) {
  const max = db.encounters.find({}, { _id: 1 }).sort({ _id: -1 }).limit(1).next()._id;
  db.counters.insertOne({ _id: "encounter", seq: max });
}

// =====================================================================
line("OPERATION 1: BOOK AN APPOINTMENT");
// =====================================================================
function bookAppointment(patientId, doctorId, start, reason) {
  const doc = db.doctors.findOne({ _id: doctorId, active: true });
  if (!doc) throw new Error("Doctor " + doctorId + " is not active");
  const lp = localParts(start);
  const end = new Date(start.getTime() + doc.consultationMinutes * 60000);
  const lpEnd = localParts(end);
  const inSession = doc.availability.some((s) => s.weekday === lp.weekday && s.start <= lp.hhmm && lpEnd.hhmm <= s.end);
  if (!inSession) throw new Error("Doctor " + doctorId + " holds no clinic at " + lp.hhmm + " on weekday " + lp.weekday);
  const pat = db.patients.findOne({ _id: patientId }, { mrn: 1, name: 1 });
  const id = db.counters.findOneAndUpdate({ _id: "encounter" }, { $inc: { seq: 1 } }, { returnDocument: "after" }).seq;
  db.encounters.insertOne({
    _id: id, patientId: patientId, mrn: pat.mrn, patientName: pat.name.first + " " + pat.name.last,
    doctor: { id: doc._id, name: "Dr " + doc.name.first + " " + doc.name.last, specialty: doc.specialty, department: doc.department.name },
    start: start, end: end, status: "Scheduled", reason: reason, bookedAt: new Date(),
    diagnoses: [], prescriptions: [], labOrders: []
  });
  return id;
}

// first free on-grid slot for Dr Raman (doctor 3) next Wednesday morning
let slot;
for (const t of ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "13:30", "14:00", "14:30", "15:00"]) {
  const c = nextWednesdayAt(t);
  if (!db.encounters.findOne({ "doctor.id": 3, start: c, status: { $ne: "Cancelled" } })) { slot = c; break; }
}
print("Booking Kevin Park (patient 19) with Dr Raman at", slot.toISOString(), "(" + localParts(slot).hhmm + " Baltimore)");
const newId = bookAppointment(19, 3, slot, "Palpitations after COVID-19");
print("-> inserted encounter _id", newId);
show(db.encounters.findOne({ _id: newId }, { patientName: 1, "doctor.name": 1, start: 1, end: 1, status: 1 }));

print("\nA second receptionist tries the SAME slot for Zara Ahmed (patient 16):");
try { bookAppointment(16, 3, slot, "Chest pain"); }
catch (e) { print("-> REJECTED: code", e.code, "-", (e.message || "").split(" collection")[0], "on index ux_doctor_slot_live"); }

print("\nLimitation: the unique index only compares exact start times. Dr Whitfield (doctor 4) uses 45-minute");
print("slots; a booking starting 15 minutes into an existing one is NOT caught by the index:");
const base = nextWednesdayAt("09:00");
db.encounters.deleteMany({ "doctor.id": 4, start: { $gte: base, $lt: new Date(base.getTime() + 3 * 3600e3) }, reason: /^Overlap test/ });
const a1 = bookAppointment(5, 4, nextWednesdayAt("13:30"), "Overlap test A");
const a2 = db.counters.findOneAndUpdate({ _id: "encounter" }, { $inc: { seq: 1 } }, { returnDocument: "after" }).seq;
db.encounters.insertOne({ _id: a2, patientId: 7, doctor: { id: 4, name: "Dr Marcus Whitfield" },
  start: nextWednesdayAt("13:45"), end: new Date(nextWednesdayAt("13:45").getTime() + 45 * 60000),
  status: "Scheduled", reason: "Overlap test B" });
print("-> both inserted:", db.encounters.countDocuments({ _id: { $in: [a1, a2] } }),
      "overlapping encounters for doctor 4. PostgreSQL's EXCLUDE constraint rejects this (Portfolio 1, test 16).");
db.encounters.deleteMany({ _id: { $in: [a1, a2] } });

// =====================================================================
line("OPERATION 2: STORE A DIAGNOSIS (multi-document ACID transaction)");
// =====================================================================
function storeDiagnosis(encounterId, dx, rxItems, by) {
  const session = db.getMongo().startSession();
  try {
    session.withTransaction(() => {
      const sdb = session.getDatabase("waynes_clinic");
      const enc = sdb.encounters.findOne({ _id: encounterId });
      if (!["CheckedIn", "Completed"].includes(enc.status))
        throw new Error("Encounter " + encounterId + " has status " + enc.status);
      const allergies = sdb.patients.findOne({ _id: enc.patientId }, { allergies: 1 }).allergies || [];
      for (const it of rxItems) {
        const hit = allergies.find((a) => a.medicationId === it.medicationId);
        if (hit) throw new Error("Allergy conflict: patient allergic to " + hit.medication + " (" + hit.severity + ")");
      }
      sdb.encounters.updateOne({ _id: encounterId }, {
        $set: { status: "Completed" },
        $push: { diagnoses: dx, prescriptions: { issuedAt: new Date(), status: "Active", items: rxItems } }
      });
      sdb.auditLog.insertOne({ at: new Date(), by: by, action: "diagnosis", encounterId: encounterId, code: dx.code });
    }, { readConcern: { level: "snapshot" }, writeConcern: { w: "majority" } });
    return "COMMITTED";
  } catch (e) {
    return "ABORTED - " + e.message;
  } finally { session.endSession(); }
}

// Sunday 25 Oct 11:00: James Holloway walks into the Emergency Department (doctor 9) and is checked in
const holloway = db.counters.findOneAndUpdate({ _id: "encounter" }, { $inc: { seq: 1 } }, { returnDocument: "after" }).seq;
db.encounters.insertOne({ _id: holloway, patientId: 1, mrn: "WCB0000001", patientName: "James Holloway",
  doctor: { id: 9, name: "Dr Naomi Fairbanks", specialty: "Emergency Medicine", department: "Emergency Medicine" },
  start: new Date("2026-10-25T15:00:00Z"), end: new Date("2026-10-25T15:15:00Z"), status: "CheckedIn",
  reason: "Walk-in: sore throat and fever", bookedAt: new Date("2026-10-25T14:52:00Z"),
  diagnoses: [], prescriptions: [], labOrders: [] });

print("James Holloway (allergic to Penicillin V) checked in, encounter", holloway);
print("Attempt 1 - diagnosis + penicillin in one transaction:");
print("->", storeDiagnosis(holloway, { code: "J02.9", description: "Acute pharyngitis, unspecified", type: "Primary", diagnosedAt: new Date() },
      [{ line: 1, medicationId: 12, medication: "Penicillin V Potassium", dosage: "500 mg", frequency: "Four times daily", days: 10, quantity: 40 }], "Dr Naomi Fairbanks"));
show(db.encounters.findOne({ _id: holloway }, { status: 1, diagnoses: 1, prescriptions: 1 }));
print("Attempt 2 - same diagnosis with azithromycin:");
print("->", storeDiagnosis(holloway, { code: "J02.9", description: "Acute pharyngitis, unspecified", type: "Primary", diagnosedAt: new Date() },
      [{ line: 1, medicationId: 16, medication: "Azithromycin", dosage: "250 mg", frequency: "Once daily", days: 5, quantity: 6 }], "Dr Naomi Fairbanks"));
show(db.encounters.findOne({ _id: holloway }, { status: 1, "diagnoses.code": 1, "prescriptions.items.medication": 1 }));
print("audit rows for this encounter:", db.auditLog.countDocuments({ encounterId: holloway }));

// =====================================================================
line("OPERATION 3: RETRIEVE PATIENT HISTORY (explain before / after index)");
// =====================================================================
const histFilter = { patientId: 3, start: { $lte: new Date("2026-10-28T23:59:59Z") } };
try { db.encounters.dropIndex("ix_patient_start"); } catch (e) { /* first run: index not created yet */ }
function explainHistory(label) {
  const ex = db.encounters.find(histFilter).sort({ start: -1 }).explain("executionStats");
  const st = ex.executionStats;
  const stage = JSON.stringify(ex.queryPlanner.winningPlan).match(/"stage":"(COLLSCAN|IXSCAN)"/)[1];
  print(`${label}: stage=${stage}  docsExamined=${st.totalDocsExamined}  keysExamined=${st.totalKeysExamined}  ` +
        `returned=${st.nReturned}  time=${st.executionTimeMillis} ms`);
}
explainHistory("Before index");
db.encounters.createIndex({ patientId: 1, start: -1 }, { name: "ix_patient_start" });
explainHistory("After index ");

print("\nTyrone Jackson's history - ONE indexed read, no joins (each encounter carries its clinical records):");
db.encounters.aggregate([
  { $match: histFilter },
  { $sort: { start: -1 } },
  { $project: { _id: 0, date: { $dateToString: { date: "$start", format: "%Y-%m-%d", timezone: "America/New_York" } },
                doctor: "$doctor.name", status: 1,
                diagnoses: "$diagnoses.code",
                medications: { $reduce: { input: "$prescriptions.items", initialValue: [],
                                          in: { $concatArrays: ["$$value", { $map: { input: "$$this", as: "i", in: "$$i.medication" } }] } } },
                labs: { $reduce: { input: "$labOrders.results", initialValue: [],
                                   in: { $concatArrays: ["$$value", { $map: { input: "$$this", as: "r",
                                         in: { $concat: ["$$r.test", "=", { $toString: "$$r.value" }, " ", { $ifNull: ["$$r.flag", ""] }] } } }] } } } } }
]).forEach((d) => print(JSON.stringify(d)));

// =====================================================================
line("ANALYTICS: top 3 primary diagnoses per department, 2026 ($unwind/$group/$topN)");
// =====================================================================
const t0 = Date.now();
db.encounters.aggregate([
  { $match: { status: "Completed", start: { $gte: new Date("2026-01-01T05:00:00Z"), $lt: new Date("2027-01-01T05:00:00Z") } } },
  { $unwind: "$diagnoses" },
  { $match: { "diagnoses.type": "Primary" } },
  { $group: { _id: { dept: "$doctor.department", code: "$diagnoses.code" }, cases: { $sum: 1 } } },
  { $group: { _id: "$_id.dept", top3: { $topN: { n: 3, sortBy: { cases: -1 }, output: { code: "$_id.code", cases: "$cases" } } } } },
  { $sort: { _id: 1 } }
]).forEach((d) => print(d._id.padEnd(35), d.top3.map((x) => x.code + " (" + x.cases + ")").join(", ")));
print("aggregation time:", Date.now() - t0, "ms");

// =====================================================================
line("SCHEMA VALIDATION: the document model still enforces rules");
// =====================================================================
try {
  db.patients.insertOne({ _id: 999999, mrn: "12345", name: { first: "Bad", last: "Record" }, dateOfBirth: new Date("1990-01-01"), sex: "F" });
} catch (e) {
  print("-> REJECTED: code", e.code, "(DocumentValidationFailure) -", e.errInfo.details.schemaRulesNotSatisfied[0].propertiesNotSatisfied
        .map((p) => p.propertyName + " " + p.details[0].reason).join("; "));
}
try {
  db.encounters.updateOne({ _id: holloway }, { $push: { diagnoses: { code: "NOT-A-CODE", type: "Primary" } } });
} catch (e) { print("-> REJECTED: code", e.code, "(DocumentValidationFailure) - diagnosis code fails the ICD-10 pattern"); }

line("COLLECTION STATS");
for (const c of ["doctors", "patients", "encounters"]) {
  const s = db.getCollection(c).stats();
  print(c.padEnd(11), String(s.count).padStart(8), "docs  avg", String(Math.round(s.avgObjSize)).padStart(5),
        "bytes  indexes:", Object.keys(s.indexSizes).join(", "));
}
