# Postmortem: duplicate journal entries (QM-142)

Author: Tomas · Incident date: Friday 2026-09-04 · Severity: high (no data was lost)

## What happened

Between 09:10 and 11:40 UTC, 31 of the 40 beta devices showed duplicate check-ins after syncing.
Some habits showed the same day checked twice, which broke streak counts.

## Root cause

Device clock skew. The merge step deduplicated events by their wall-clock timestamp. When two
devices on the same account had clocks more than 90 seconds apart, the same check-in arrived
with two different timestamps and was kept twice.

## Fix

Deduplication now uses the event ID plus a Lamport clock instead of wall-clock time. Shipped in
build 0.9.7 on 2026-09-06. A one-time cleanup removed the duplicates on every affected device.

## Follow-ups

1. Add a sync test that runs two simulated devices with clocks 10 minutes apart. (done)
2. Show a "syncing…" state instead of a streak count while a merge is running. (Priya)
3. Add "data loss" and "duplicate data" as separate severities in the bug tracker. (Maya)
