from typing import Optional, List
from pydantic import BaseModel, Field

class ProjectCreateRequest(BaseModel):
    name: str = Field(..., description="Project name")
    repoUrl: Optional[str] = None
    localPath: Optional[str] = None

class TaskTransitionRequest(BaseModel):
    status: str
    instruction: Optional[str] = ""
    auto_recovery_prompt: Optional[bool] = True

class BulkRequeueRequest(BaseModel):
    task_ids: List[str]

class BulkUpdateRequest(BaseModel):
    ids: List[str]
    action: str
    repo: Optional[str] = ""
