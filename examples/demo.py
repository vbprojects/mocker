"""Record: mocker forward --runner python --trace 'get_name_*' --cassette demo.json -- python examples/demo.py"""
def get_name_user(user_id=42):
    return {"user_id": user_id, "name": "Original"}


print(get_name_user())
