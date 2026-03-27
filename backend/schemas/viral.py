from pydantic import BaseModel


class ViralVideoRequest(BaseModel):
    api_key: str
    hours: int
    region: str


class ViralVideoResponse(BaseModel):
    video_id: str
    title: str
    views: int
    likes: int
    comments: int
    engagement_pct: float
    url: str
