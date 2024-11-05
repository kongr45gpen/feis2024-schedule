import argparse
import requests
import logging
from rich import print
from rich.logging import RichHandler
from jinja2 import Environment, FileSystemLoader, select_autoescape
from datetime import datetime, time, date, timedelta
import dateutil.parser
import pytz
import copy
import yaml
import csv

FORMAT = "%(message)s"
logging.basicConfig(
    level="NOTSET", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("rich")

parser = argparse.ArgumentParser(description='Generate a LaTeX schedule from a frab-compatible .json file')

args = parser.parse_args()

log.info("Welcome to the pretalx rich handler!")

# Schedule in FRAB format
data = requests.get('https://pretalx.electroserv.eu/feis2024/schedule/export/schedule.json')
with open('frab.json', 'w') as f:
    f.write(data.text)
json = data.json()

# Schedule in original pretalx format
# This is needed for non-submission slots (breaks) that are not exported to the frab schedule
data_aux = requests.get('https://pretalx.electroserv.eu/feis2024/schedule/widgets/schedule.json')
with open('schedule.json', 'w') as f:
    f.write(data_aux.text)
json_aux = data_aux.json()


# Answers from authors from CSV file
speaker_answers = {}
with open('feis2024-speaker-questions.csv', newline='') as csvfile:
    reader = csv.DictReader(csvfile)
    for row in reader:
        if not row['code'] in speaker_answers:
            speaker_answers[row['code']] = {}
            speaker_answers[row['code']]['name'] = row['name']
        speaker_answers[row['code']][row['question']] = row['answer']

# Parse JSON
days = json['schedule']['conference']['days']
timezone = pytz.timezone(json['schedule']['conference']['time_zone_name'])

for day in days:
    day['date_datetime'] = datetime.strptime(day['date'], "%Y-%m-%d")

    for room in day['rooms'].values():
        for event in room:
            # Generate event end
            start = datetime.strptime(event['start'],"%H:%M")
            duration = datetime.strptime(event['duration'],"%H:%M")
            delta = timedelta(hours=duration.hour, minutes=duration.minute)
            end = start + delta
            event['end'] = end.strftime("%H:%M")

            event['start_datetime'] = timezone.localize(datetime.combine(day['date_datetime'], start.time()))
            event['end_datetime'] = timezone.localize(datetime.combine(day['date_datetime'], end.time()))

            # Get event colour based on its track
            #for track in json['schedule']['conference']['tracks']:
            #    if track['track_id'] == event['track_id']:
            #        event['color'] = track['color']
            # The above code but more pythonic
            #event['color'] = next((track['color'] for track in json['schedule']['conference']['tracks'] if track['track_id'] == event['track_id']), None)
            track = next(filter(lambda track: track['name'] == event['track'], json['schedule']['conference']['tracks']), None)
            if track:
                event['color'] = track['color']
            else:
                log.warning(f"Track {event['track']} not found for talk {event['title']}.")
                event['color'] = '#7f2ca0'

            event['code'] = event['url'].split('/')[-2]

            for speaker in event['persons']:
                try:
                    speaker['affiliation'] = speaker_answers[speaker['code']]['Affiliation']
                except KeyError as e:
                    log.warning(f"Speaker {speaker['public_name']} has unknown affiliation")

# Parse aux JSON for breaks
for talk in json_aux['talks']:
    if 'duration' not in talk and 'state' not in talk:
        name = talk['title']['en']
        start = dateutil.parser.parse(talk['start'])
        end = dateutil.parser.parse(talk['end'])
        duration = end - start

        start_short = start.astimezone(timezone).strftime("%H:%M")
        end_short = end.astimezone(timezone).strftime("%H:%M")

        # Shove break into the schedule, right before the first session whose start time is after the end of the break
        for day in days:
            date = day['date_datetime']

            # Make sure the date is the same
            if date.date() != start.date():
                continue

            for room in day['rooms'].values():
                for event in room:
                    # print(f"Comparing {event['start_datetime']} to {start}")

                    event_start = event['start_datetime']

                    if event_start > start:
                        break_event = {
                            "break": True,
                            "title": name,
                            "track": "Break",
                            "start": start_short,
                            "end": end_short,
                            "start_datetime": start,
                            "end_datetime": end,
                            "code": ""
                        }
                        room.insert(room.index(event), break_event)

                        break

# Apply overrides
try:
    with open('overrides.yml') as f:
        overrides = yaml.safe_load(f)
except Exception as e:
    log.warning(f"Failed to read overrides: {e}")


for day in days:
    for room in day['rooms'].values():
        for event in room:
            if event['code'] in overrides['talks']:
                override = overrides['talks'][event['code']]
                for key in override:
                    event[key] = override[key]
                
                if 'persons_reorder' in override:
                    def get_person_by_code(persons, code):
                        result = next(filter(lambda person: person['code'] == code, persons), None)
                        if not result:
                            log.fatal(f"For the talk \"{event['title']}\" ({ event['code'] }), the person with code \"{code}\" was not found in the list of persons.")
                        return result

                    original_order = [ person['code'] for person in event['persons'] ]
                    event['persons'] = [ get_person_by_code(event['persons'], code) for code in override['persons_reorder'] ]
                    new_order = [ person['code'] for person in event['persons'] ]

                    logging.debug(f"Reordered persons for {event['title']} from {original_order} to {new_order}")
                    
            if 'persons' in event:
                for person in event['persons']:
                    if 'affiliation' in person and person['affiliation'] in overrides['affiliations']:
                        person['affiliation'] = overrides['affiliations'][person['affiliation']]

# Combine tracks
for day in days:
    day['date_datetime'] = datetime.strptime(day['date'], "%Y-%m-%d")

    for room in day['rooms'].values():
        first_track_key = -1
        keys_to_remove = []

        for key,event in enumerate(room):
            if 'track' not in event or not event['track']:
                continue
            if not event['track'].startswith("Session"):
                continue

            if (key + 1) < len(room):                
                next_event = room[key + 1]
                if event['track'] == next_event['track'] and first_track_key == -1:
                    # OK, this is the first event in this track
                    first_track_key = key
                    event['subevents'] = [ copy.deepcopy(event) ]
                    event['title'] = event['track']
                elif first_track_key != -1:
                    # We are an event in a track of events
                    room[first_track_key]['subevents'].append(event) 
                    keys_to_remove.append(key)

                    if event['track'] != next_event['track']:
                        # We are the last event in a track of events
                        logging.debug(f"Found end of track {event['track']} with {len(room[first_track_key]['subevents'])} events")
                        first_track_key = -1
        
        for key in reversed(keys_to_remove):
            del room[key]


# days = [days[1]]


env = Environment(
    loader=FileSystemLoader("."),
    autoescape=select_autoescape(['tex']),
    block_start_string='{{%',
    block_end_string='%}}',
    variable_start_string='{{{',
    variable_end_string='}}}',
    comment_start_string='{{#',
    comment_end_string='#}}',
)

# Maybe define a few Jinja2 filters
def abbreviate_name(name):
    """Abbreviates all parts of a full name except the last. E.g. John Marc S. Doe becomes J. M. S. Doe"""
    parts = name.split()
    return " ".join([f"{part[0]}." if i < len(parts) - 1 else part for i, part in enumerate(parts)])
env.filters['abbreviate_name'] = abbreviate_name

template = env.get_template('schedule.tex.jinja')

with open('schedule.tex', 'w') as f:
    f.write(template.render(json=json, days=days))

log.info("Generated schedule stored at schedule.tex")

#print(json)