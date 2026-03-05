import os
from app import app, get_db

def test_survey_submit():
    with app.test_client() as client:
        payload = {
            "rating": 4,
            "guest_type": "一人で",
            "positive_points": ["コーヒー/ドリンクが美味しい", "落ち着く雰囲気だった"],
            "negative_points": [],
            "comment": "また来ます！"
        }
        print("Sending POST to /shop/test-cafe/feedback...")
        response = client.post("/shop/test-cafe/feedback", json=payload)
        print("Status code:", response.status_code)
        
        # We need to manually decode unicode hex characters
        text = response.data.decode("utf-8")
        print("Response:", text)

if __name__ == "__main__":
    test_survey_submit()
