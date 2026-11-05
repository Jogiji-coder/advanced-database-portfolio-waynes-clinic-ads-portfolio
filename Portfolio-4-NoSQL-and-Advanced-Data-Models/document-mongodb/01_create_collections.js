// =====================================================================
// Wayne's Clinic Baltimore — Portfolio 4 · Document model (MongoDB 8)
// Script 01 · Collections, JSON-Schema validation and indexes
// Run:  mongosh "mongodb://127.0.0.1:27017/waynes_clinic" 01_create_collections.js
// =====================================================================
db = db.getSiblingDB("waynes_clinic");
db.dropDatabase();

// ---------------------------------------------------------------- doctors
// Weekly availability is EMBEDDED: it is small, bounded (<= 14 sessions)
// and always read together with the doctor when booking.
db.createCollection("doctors", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "staffNumber", "name", "specialty", "department", "consultationMinutes", "availability"],
    properties: {
      _id:                 { bsonType: "int" },
      staffNumber:         { bsonType: "string", pattern: "^WCB-D[0-9]{4}$" },
      name:                { bsonType: "object", required: ["first", "last"] },
      specialty:           { bsonType: "string" },
      department:          { bsonType: "object", required: ["name"] },
      consultationMinutes: { enum: [15, 20, 30, 45, 60] },
      availability: { bsonType: "array", maxItems: 21, items: {
        bsonType: "object", required: ["weekday", "start", "end"],
        properties: { weekday: { bsonType: "int", minimum: 1, maximum: 7 },
                      start: { bsonType: "string", pattern: "^[0-2][0-9]:[0-5][0-9]$" },
                      end:   { bsonType: "string", pattern: "^[0-2][0-9]:[0-5][0-9]$" } } } }
    } } }
});

// --------------------------------------------------------------- patients
// Allergies and the emergency contact are EMBEDDED (bounded, owned by the
// patient, needed on every prescribing decision). Encounters are NOT
// embedded: they grow without limit and are written by many clinicians.
db.createCollection("patients", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "mrn", "name", "dateOfBirth", "sex"],
    properties: {
      _id:         { bsonType: "int" },
      mrn:         { bsonType: "string", pattern: "^WCB[0-9]{7}$" },
      name:        { bsonType: "object", required: ["first", "last"] },
      dateOfBirth: { bsonType: "date" },
      sex:         { enum: ["F", "M", "X", "U"] },
      bloodGroup:  { enum: ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-", null] },
      allergies:   { bsonType: "array", items: {
        bsonType: "object", required: ["medication", "severity"],
        properties: { severity: { enum: ["Mild", "Moderate", "Severe"] } } } }
    } } },
  validationAction: "error"
});

// ------------------------------------------------------------- encounters
// One document per appointment = the aggregate that is created and
// updated as a unit: booking, diagnoses, prescriptions and lab results.
// Doctor and patient names are copied in (extended reference pattern) so
// a history screen needs no join.
db.createCollection("encounters", {
  validator: { $jsonSchema: {
    bsonType: "object",
    required: ["_id", "patientId", "doctor", "start", "end", "status"],
    properties: {
      _id:       { bsonType: ["int", "long"] },
      patientId: { bsonType: "int" },
      doctor:    { bsonType: "object", required: ["id", "name"] },
      start:     { bsonType: "date" },
      end:       { bsonType: "date" },
      status:    { enum: ["Scheduled", "CheckedIn", "Completed", "Cancelled", "NoShow"] },
      diagnoses: { bsonType: "array", items: {
        bsonType: "object", required: ["code", "type"],
        properties: { code: { bsonType: "string", pattern: "^[A-Z][0-9][0-9A-Z](\\.[0-9A-Z]{1,4})?$" },
                      type: { enum: ["Primary", "Secondary", "Differential"] } } } },
      prescriptions: { bsonType: "array" },
      labOrders:     { bsonType: "array" }
    } } }
});

db.createCollection("auditLog");
print("collections created:", db.getCollectionNames().sort().join(", "));
