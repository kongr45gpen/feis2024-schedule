import argparse
import requests
import logging
from rich import print
from rich.console import Console
from rich.logging import RichHandler
from jinja2 import Environment, FileSystemLoader, select_autoescape
from datetime import datetime, time, date, timedelta
import dateutil.parser
import pytz
import copy
import yaml
import csv
import xlsxwriter
import os
import json
from dotenv import load_dotenv

parser = argparse.ArgumentParser(description='Generate a LaTeX schedule from a frab-compatible .json file')
parser.add_argument('-r', '--refresh', action='store_true', help='Refresh the schedule online instead of using files')
parser.add_argument('-p', '--presentation', action='store_true', help='Download uploaded presentation files')
parser.add_argument('-d', '--debug', action='store_true', help='Enable debug logging')
args = parser.parse_args()

FORMAT = "%(message)s"
logging.basicConfig(
    level="NOTSET" if args.debug else "INFO", format=FORMAT, datefmt="[%X]", handlers=[RichHandler()]
)

log = logging.getLogger("rich")

load_dotenv()

console = Console()

log.info("Welcome to the pretalx rich handler!")

if args.refresh:
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
else:
    with open('frab.json') as f:
        json = yaml.safe_load(f)
    with open('schedule.json') as f:
        json_aux = yaml.safe_load(f)

# Session answers
if os.getenv('PRETALX_API_KEY') and args.refresh:
    headers = {
        'Authorization': f'Token {os.getenv("PRETALX_API_KEY")}'
    }
    url = 'https://pretalx.electroserv.eu/feis2024/schedule/export/submission-questions.csv'
    answers = requests.get(url, headers=headers)
    with open('feis2024-submission-questions.csv', 'wb') as f:
        f.write(answers.content)

    url = 'https://pretalx.electroserv.eu/feis2024/schedule/export/speaker-questions.csv'
    answers = requests.get(url, headers=headers)
    with open('feis2024-speaker-questions.csv', 'wb') as f:
        f.write(answers.content)

    url = 'https://pretalx.electroserv.eu/api/events/feis2024/submissions/'
    submission_info = []
    while url:
        submission_json = requests.get(url, headers=headers).json()
        submission_info += submission_json['results']
        url = submission_json['next']
    
    with open('feis2024-submission-info.yml', 'w') as f:
        yaml.dump(submission_info, f)

# Speaker answers


# Answers from authors from CSV file
speaker_answers = {}
with open('feis2024-speaker-questions.csv', newline='') as csvfile:
    reader = csv.DictReader(csvfile)
    for row in reader:
        if not row['code'] in speaker_answers:
            speaker_answers[row['code']] = {}
            speaker_answers[row['code']]['name'] = row['name']
        speaker_answers[row['code']][row['question']] = row['answer']

session_answers= {}
with open('feis2024-submission-questions.csv', newline='') as csvfile:
    reader = csv.DictReader(csvfile)
    for row in reader:
        if not row['code'] in session_answers:
            session_answers[row['code']] = {}
        session_answers[row['code']][row['question']] = row['answer']

submission_details = {}
with open('feis2024-submission-info.yml') as f:
    submission_details = yaml.safe_load(f)

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

            if event['code'] in session_answers:
                event['answers'] = session_answers[event['code']]

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

        break_event = {
            "break": True,
            "title": name,
            "track": "Break",
            "start": start_short,
            "end": end_short,
            "start_datetime": start,
            "end_datetime": end,
            "code": f"BRK-{start.astimezone(timezone).weekday()}-{start_short.replace(':', '')}",
        }

        # Shove break into the schedule, right before the first session whose start time is after the end of the break
        for day in days:
            date = day['date_datetime']

            # Make sure the date is the same
            if date.date() != start.date():
                continue

            added = False
            for room in day['rooms'].values():

                for event in room:

                    event_start = event['start_datetime']

                    if event_start > start:
                        room.insert(room.index(event), break_event)
                        added = True

                        break
                
                if not added:
                    # If we reach this point, it means that the break is the last event of the day
                    room.append(break_event)

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

# Create xlsx files for OnTime
workbook = xlsxwriter.Workbook(f"output/ontime_{datetime.now()}.xlsx".replace(":", "-"))
debug_json = {}
for day in days:
    for room_name, room in day['rooms'].items():
        worksheet = workbook.add_worksheet(f'{day["date"]} {day["date_datetime"].strftime("%A")} {room_name}'[0:31])
        worksheet.write(0, 0, "time start")
        worksheet.write(0, 1, "time end")
        worksheet.write(0, 2, "time end actual")
        worksheet.write(0, 3, "title")
        worksheet.write(0, 4, "Speakers")
        worksheet.write(0, 5, "Track")
        worksheet.write(0, 6, "Code")
        worksheet.write(0, 7, "colour")
        worksheet.write(0, 8, "avatar")
        worksheet.write(0, 9, "notes")
        worksheet.write(0, 10, "address")
        worksheet.write(0, 11, "public")

        for i, event in enumerate(room):
            row = []
            row.append(event['start'])
            # The supposed end time should be around 75% of the actual end time to allow for questions and Q&A 
            if event['track'] == "Keynote":
                qa = timedelta(minutes=10)
            elif event['track'] and event['track'].startswith("Session"):
                qa = timedelta(minutes=5)
            else:
                qa = timedelta(minutes=0)
            row.append((event['end_datetime'] - qa).strftime("%H:%M"))
            row.append(event['end'])
            row.append(event['title'])
            if 'persons' in event:
                row.append(", ".join([person['public_name'] for person in event['persons']]))
            else:
                row.append("")
            row.append(event['track'])
            row.append(event['code'])
            row.append(event['color'] if 'color' in event else '')
            if 'persons' in event and len(event['persons']) > 0 and 'avatar' in event['persons'][0] and event['persons'][0]['avatar']:
                row.append(event['persons'][0]['avatar'])
            else:
                row.append("")

            technical = ""
            # Get (internal) notes from submission details
            submission = next(filter(lambda submission: submission['code'] == event['code'], submission_details), None)
            if submission:
                for attribute in ['notes', 'internal_notes']:
                    if attribute in submission and submission[attribute] and submission[attribute] != "":
                        technical += f"{submission[attribute]}. "
            elif event['code'] and not event['code'].startswith("BRK-"):
                log.warning(f"Submission {event['code']} not found in submission details")

            # Get question answers (from speakers) from csv file
            q = ['Are you using your own laptop?', 'Does your presentation include audio?', 'Do you have a microphone preference?']
            for question in q:
                try:
                    technical += f"{question.split()[-1].capitalize()}: {event['answers'][question]}. "
                except KeyError:
                    pass
                except TypeError:
                    pass

            if technical != "":
                log.debug(f"Technical details for {event['title']}: `{technical}`")
            
            row.append(technical)

            # Speaker way of addressing
            if 'persons' in event and len(event['persons']) > 0:
                code = event['persons'][0]['code']
                address = event['persons'][0]['public_name']
                if code in speaker_answers:
                    their_answers = speaker_answers[code]
                    if 'Gender' in their_answers:
                        address += f", {their_answers['Gender']}"
                    if 'Affiliation' in their_answers:
                        address += f" ({their_answers['Affiliation']})"
                    if 'Title' in their_answers:
                        address = f"{their_answers['Title']} {address}"
                    else:
                        address = f". {address}"
                row.append(address)
                log.debug(f" ↳ Address: {address}")
            else:
                row.append("")

            row.append("1")


            for j, cell in enumerate(row):
                worksheet.write(i + 1, j, cell)
            
            debug_json[event['code'] if 'code' in event else ''] = row
workbook.close()

with open('output/ontime.yml', 'w') as f:
    yaml.dump(debug_json, f)

# Create featured events
featured = []
for code, override in overrides['featured'].items():
    found = False
    this_event = None
    for day in days:
        for room in day['rooms'].values():
            for event in room:
                if event['code'] == code:
                    this_event = event
                    found = True
                    break
            if found:
                break
        if found:
            break
    if not found:
        log.warning(f"Featured event {code} not found in schedule")
        continue

    # Apply overrides, if any
    this_event['day'] = day['date_datetime']
    this_event = this_event | override

    featured.append(this_event)

# Download presentation files
if args.presentation:
    for day in days:
        for room in day['rooms'].values():
            for event in room:
                if 'color' in event and 'track' in event:
                    track = str(event['track'])[:30]
                    console.print(f'{track: >30}', style=event['color'], end=' ')
                else:
                    track = ""
                    console.print(f'{track: >30}', style='', end=' ')
                
                title = event['title'][:40]
                console.print(f'{title: <40}', end=' ')

                # Try to find if they have a laptop
                if 'answers' in event and 'Are you using your own laptop?' in event['answers']:
                    if event['answers']['Are you using your own laptop?'].startswith("I would like"):
                        console.print(" [ OUR ]", end=' ')
                    elif event['answers']['Are you using your own laptop?'].startswith("I will bring"):
                        console.print(" [yellow][THEIR][/yellow]", end=' ')
                    else:
                        console.print(" [  ?  ]", end=' ')
                else:
                    console.print(" [  ?  ]", end=' ')

                # Try to find if this event has a presentation
                if 'answers' in event and 'Presentation File' in event['answers']:
                    url = "https://pretalx.electroserv.eu" + event['answers']['Presentation File']

                    filename = url.split('/')[-1]
                    try:
                        extension = filename.split('.')[1].upper()
                    except Exception as e:
                        extension = ""
                    
                    # If the filename exists, keep the presentation. If it doesn't, download it
                    if not os.path.exists(f"presentations/{filename}"):
                        log.debug(f"Downloading {url} ...")
                        try:
                            r = requests.get(url, headers = {
                                'Authorization': f'Token {os.getenv("PRETALX_API_KEY")}'
                            })
                            r.raise_for_status()
                        except Exception as e:
                            log.error(f"Failed to download {url}: {e}")
                            continue
                        with open(f"presentations/{filename}", 'wb') as f:
                            f.write(r.content)
                        log.debug("Download complete")
                        console.print(f" [blue]downloaded [bold]new {extension}[/bold] presentation named {filename}[/blue]")
                    else:
                        console.print(f" [green]has [bold]{extension}[/bold] presentation named {filename}[/green]")

                elif 'answers' in event and 'Presentation File (link)' in event['answers']:
                    url = event['answers']['Presentation File (link)']
                    
                    console.print(f" has [cyan]URL presentation at:[/cyan] {url}")

                    # Create a shortcut file that contains the URL...
                    with open(f"presentations/{event['title']}.url", 'w') as f:
                        f.write("[InternetShortcut]\nURL=" + url)
                else:
                    console.print(" has [bold red]no presentation[/bold red]")

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


#days = [days[1]]
#days = []


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

def latexize(text):
    if not text:
        text = ""
    return text.replace('&', '\\&').replace('%', '\\%').replace('#', '\\#').replace('_', '\\_') \
        .replace('Prof. ', 'Prof.~') \
        .replace('Dr. ', 'Dr.~')
env.filters['latexize'] = latexize

template = env.get_template('schedule.tex.jinja')

with open('schedule.tex', 'w') as f:
    f.write(template.render(json=json, days=days, featured=featured, overrides=overrides))

log.info("Generated schedule stored at schedule.tex")

#print(json)