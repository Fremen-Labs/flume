# Dispatcher Plan

## Immediate next behavior

1. Intake prompt becomes one or more work items
2. PM agent decomposes into feature/story/task tree
3. Dispatcher marks leaf tasks `ready`
4. Implementer/tester/reviewer wrappers consume leaf tasks
5. New bugs/tasks can be created under parents

## Near-term automation
- add claim/unclaim helpers
- add dependency satisfaction checks
- auto-route approved parents when all children are done
- auto-create bug children from reviewer findings
