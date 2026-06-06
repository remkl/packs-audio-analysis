from pydantic import BaseModel


class AnalysisResult(BaseModel):
    bpm: float
    bpm_confidence: float
    key_standard: str
    key_camelot: str
    key_confidence: float
    duration: float


class AnalyzeRequest(BaseModel):
    filename: str


class UploadResponse(BaseModel):
    filename: str
