from pydantic import BaseModel


class ClaimRequest(BaseModel):
    claim_id: str


class ClaimResponse(BaseModel):
    prediction: str
