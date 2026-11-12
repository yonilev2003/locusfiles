# Release checklist

Used for every build that goes to testers or to the stores.

- [ ] All sync tests pass, including the two-device clock-skew test.
- [ ] Migration of the local SQLite schema tested on a copy of a real beta database.
- [ ] Crash-free sessions in the previous build are above 99.5%.
- [ ] Version number and build number bumped in both mobile apps and the web app.
- [ ] Release notes written in plain language (what changed for the person, not for us).
- [ ] Store screenshots still match the app.
- [ ] Support FAQ checked for anything the release changes.
- [ ] Rollback plan written: which build to go back to and who presses the button.

Owner of the checklist: Tomas. Anyone can block a release by leaving an item unchecked with a
reason.
