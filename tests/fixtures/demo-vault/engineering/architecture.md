# Quillwend architecture

Owner: Tomas · Last reviewed: 2026-09-09

## Overview

Quillwend is offline first. Every device keeps the full journal in a local SQLite database. The
sync service is a small relay that moves encrypted change events between one person's own
devices; it never needs to read an entry to do its job.

## Data model

A journal is a list of habits. Each habit has a daily log of check-ins and optional notes. Every
change (a new check-in, an edited note, a deleted habit) is written as an append-only event with:

- an event ID (a random 128-bit value created on the device),
- the device ID,
- a Lamport clock value,
- the payload, encrypted with the account's content key.

The current state of the journal is rebuilt by replaying events in Lamport order. Two events with
the same event ID are the same event, which is what makes replays and retries safe.

## Sync

Devices push their new events and pull everything they have not seen since their last cursor.
Sync runs when the app opens, when it returns to the foreground, and every 15 minutes while it is
open. There is no background sync on iOS yet.

Before build 0.9.7, deduplication used wall-clock timestamps. That caused the QM-142 incident:
see the postmortem in this folder.

## Shared streaks

A shared streak is a separate, small journal with its own content key, shared between 2 and 8
members. Members only see check-ins for the shared habit, never each other's private journals.

## Encryption

- Content keys are created on the first device and shared with new devices through a
  device-to-device pairing code.
- The relay stores ciphertext and routing metadata (account ID, device ID, event size, time).
- Losing every device and the recovery code means losing the journal. This is deliberate.

## Limits

Request and payload limits for the sync API are in api-limits.txt in this folder.
