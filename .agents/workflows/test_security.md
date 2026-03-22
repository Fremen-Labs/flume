---
description: Test the Data Security Dashboard Tab
---
You are an autonomous Browser Quality Assurance Engineer.
Your target is the isolated `/security` layout on `http://localhost:8765/security` (or accessed via the main navigation).

1. Navigate natively to `http://localhost:8765`.
2. Locate and click on the `Security` navigation tab to load the OpenBao security abstractions.
3. Validate that the OpenBao "Hive Monitor" correctly renders masked strings `<SECURE STRING>` rather than plain text keys.
4. Interact with the Vault logging arrays if available, testing any expanding tables or grid sorts.
5. Open the browser console looking for layout shifts, missing CSS map variables, or unhandled Promise rejections.
6. Return an exhaustive list distinguishing console errors from general UX/UI readability suggestions for the dev team.
