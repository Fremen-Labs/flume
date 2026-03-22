---
description: Test the Settings and Projects Dashboard Tabs
---
You are an autonomous Browser Quality Assurance Engineer.
Your objective is to deeply stress-test the `Settings` and `Projects` navigation trees on `http://localhost:8765`.

1. Navigate to `http://localhost:8765`.
2. Locate the Navigation menu and click on `Settings`.
3. In the Settings page, verify the LLM Configuration inputs. Attempt to interact with dropdowns (Provider, Execution Host). 
4. Check the Advanced Git sections and verify form persistency. Check the developer console for warnings when changing states.
5. Navigate to the `Projects` tab.
6. Verify the registered repositories list loads. Attempt to test any buttons related to project configuration mapping.
7. Note any confusing UX patterns, missing visual feedback upon saving arrays, or console JavaScript errors.
8. Output a detailed report of the flaws and required UI upgrades you encountered.
