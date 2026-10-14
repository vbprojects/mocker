"""Record three name outcomes, then sample them in reverse with sampling.json."""
from itertools import cycle

names = cycle(['Ryan', 'Alex', 'Sam'])


def get_name_user(user_id=42):
    return {'user_id': user_id, 'name': next(names)}


for _ in range(3):
    print(get_name_user())
