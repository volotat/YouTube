from src.db_models import db


class YoutubeLibrary(db.Model):
    id = db.Column(db.Integer, unique=True, primary_key=True)
    hash = db.Column(db.String, nullable=True, unique=True)
    hash_algorithm = db.Column(db.String, nullable=True, default=None)
    file_path = db.Column(db.String, nullable=True)
    url = db.Column(db.String, nullable=True, index=True)
    user_rating = db.Column(db.Float, nullable=True)
    user_rating_date = db.Column(db.DateTime, nullable=True)
    model_rating = db.Column(db.Float, nullable=True)
    model_hash = db.Column(db.String, nullable=True)
    last_viewed = db.Column(db.DateTime, nullable=True, default=None)

    def as_dict(self):
        result = {}
        for column in self.__table__.columns:
            val = getattr(self, column.name)
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            result[column.name] = val
        return result
