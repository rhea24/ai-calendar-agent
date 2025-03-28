import base64
from datetime import datetime, timedelta
from typing import Optional, Literal
from pydantic import BaseModel, Field
from openai import OpenAI
import os
import logging
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import pytz

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# set up model
OPENAI_API_KEY = "INSERT_API_KEY_HERE"
client = OpenAI(api_key = OPENAI_API_KEY)
model = "gpt-4o"

# scope of access for Google APIs
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.readonly"]

def get_credentials():
    """Get credentials for Google APIs"""
    creds = None

    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)

        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    
    return creds


def addEventToCal(name, location, description, startTime, endTime, attendees):
    """Add event with specified details to calendar (using EST for simplicity)"""
    creds = get_credentials()
    service = build('calendar', 'v3', credentials=creds)

    event = {
        'summary': name,
        'location': location,
        'description': description,
        'start': {
            'dateTime': startTime, # '2025-03-28T09:00:00-04:00'
        },
        'end': {
            'dateTime': endTime, # '2025-03-28T17:00:00-04:00'
        },
        'attendees': attendees, # [ {'email': 'attendee1@example.com'}, {'email': 'attendee2@example.com'}]
        'reminders': {
            'useDefault': False,
            'overrides': [
                {'method': 'popup', 'minutes': 10},
            ],
        },
    }

    event = service.events().insert(calendarId='288911f72c8e08c7b39e018928dc5253bf2e8316cbb81b5d2321b9c990028ea6@group.calendar.google.com', body=event).execute()


# ---------------------------------------------------------------------------
# Data models for routing and responses; to control the flow of application
# ---------------------------------------------------------------------------


class CalendarRequestType(BaseModel):
    """Router LLM call: Determine the type of calendar request"""

    request_type: Literal["new_event", "other"] = Field(
        description="Type of calendar request being made"
    )
    confidence_score: float = Field(description="Confidence score between 0 and 1")
    description: str = Field(description="Cleaned description of the request")


class NewEventDetails(BaseModel):
    """Details for creating a new event"""

    name: str = Field(description="Name of the event")
    date: str = Field(description="Date and time of the event (ISO 8601)")
    duration_minutes: Optional[int] = Field(description="Duration in minutes")
    # participants: list[str] = Field(description="List of participants")
    location: Optional[str] = Field(description="Location of the event")
    description: str = Field(description="Description of the event")

class CalendarResponse(BaseModel):
    """Final response format"""

    success: bool = Field(description="Whether the operation was successful")
    message: str = Field(description="User-friendly response message")
    calendar_link: Optional[str] = Field(description="Calendar link if applicable")


# determine whether we need to add a calendar event based on the input
def route_calendar_request(user_input: str) -> CalendarRequestType:
    """Router LLM call to determine the type of calendar request"""
    logger.info("Routing calendar request")

    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Determine if this text includes a request to schedule a new calendar event. "
                    "The user may explicitly mention 'schedule', 'set up', or 'add an event', "
                    "but they might also imply it by suggesting a time for a conversation, meeting, or discussion. "
                    "Examples of implicit event requests include: "
                    "'Are you free to chat at 3 PM?' or 'Let's catch up on Monday afternoon'. "
                    "Consider context when deciding if this is an event request.",
            },
            {"role": "user", "content": user_input},
        ],
        response_format=CalendarRequestType,
    )
    result = completion.choices[0].message.parsed
    logger.info(
        f"Request routed as: {result.request_type} with confidence: {result.confidence_score}"
    )
    return result


def handle_new_event(description: str, sender: str) -> CalendarResponse:
    """Process a new event request"""
    logger.info("Processing new event request")

    today = datetime.now()
    date_context = f"Today is {today.strftime('%A, %B %d, %Y')}."

    # Get event details
    completion = client.beta.chat.completions.parse(
        model=model,
        messages=[
            {
                "role": "system",
                "content": f"{date_context} Extract details for creating a new calendar event. When dates reference 'next Tuesday' or similar relative dates, use this current date as reference.",
            },
            {"role": "user", "content": description},
        ],
        response_format=NewEventDetails,
    )
    details = completion.choices[0].message.parsed

    logger.info(f"New event: {details.model_dump_json(indent=2)}")

    event_timezone = pytz.timezone("America/New_York") # for simplicity
    dt_obj = datetime.fromisoformat(details.date).astimezone(event_timezone)

    endtime = None
    if details.duration_minutes is None:
        endtime = dt_obj + timedelta(minutes=60)
    else:
        endtime = dt_obj + timedelta(minutes=details.duration_minutes)
    
    # actually add event to calendar
    addEventToCal(details.name, details.location or "", details.description, dt_obj.isoformat(), endtime.isoformat(), [{'email': sender}])

    return CalendarResponse(
        success=True,
        message=f"Created new event '{details.name}' for {details.date} with {sender}",
        calendar_link=f"calendar://new?event={details.name}",
    )

def process_calendar_request(user_input: str, sender: str) -> Optional[CalendarResponse]:
    """Main function implementing the routing workflow"""
    logger.info("Processing calendar request")

    route_result = route_calendar_request(user_input)

    if route_result.confidence_score < 0.7:
        logger.warning(f"Low confidence score: {route_result.confidence_score}")
        return None

    if route_result.request_type == "new_event":
        return handle_new_event(route_result.description, sender)
    else:
        logger.warning("Request type not supported")
        return None

def process_new_messages():
    creds = get_credentials()
    service = build('gmail', 'v1', credentials=creds)
    # Get list of unread messages
    results = service.users().messages().list(
        userId='me', labelIds=['INBOX', 'UNREAD'], maxResults=10).execute()
    messages = results.get('messages', [])
    
    for message in messages:
        msg = service.users().messages().get(userId='me', id=message['id']).execute()
        # Process message data here
        process_calendar_request(get_message_body(msg), get_sender(msg))
        
def get_message_body(message):
    if 'parts' in message['payload']:
        # Multipart message
        for part in message['payload']['parts']:
            if part['mimeType'] == 'text/plain':
                if 'data' in part['body']:
                    return base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
    elif 'body' in message['payload'] and 'data' in message['payload']['body']:
        # Simple message
        return base64.urlsafe_b64decode(message['payload']['body']['data']).decode('utf-8')
    return ""

def get_sender(message):
    headers = message['payload']['headers']
    for header in headers:
        if header['name'] == 'From':
            return header['value']
    return "(Unknown sender)"

def main():
    get_credentials()
    process_new_messages()

if __name__ == '__main__':
    main()



# ----------old test examples-----------

# new_event_input = "Let's schedule a team meeting next Tuesday at 2pm with Alice and Bob"
# result = process_calendar_request(new_event_input)
# if result:
#     print(f"Response: {result.message}")

# new_event_input2 = "Can we meet Thursday afternoon at 4 with Alice?"
# result = process_calendar_request(new_event_input2)
# if result:
#     print(f"Response: {result.message}")

# new_event_input3 = "Hey Sarah! Nice to hear from you. It's been a while. Catching up would be nice. How about 2pm on Wednesday?"
# result = process_calendar_request(new_event_input3)
# if result:
#     print(f"Response: {result.message}")

# new_event_input4 = "Dear Bob, I hope you're doing well. I wanted to update you -- I went to California this week and met up with John! It was such a cool experience and would love to tell you more about it. Are you free on Monday afternoon to talk about it around 4pm?"
# result = process_calendar_request(new_event_input4)
# if result:
#     print(f"Response: {result.message}")

# new_event_input5 = "Let's chat for 45 minutes at 4pm on Wednesday. We can meet at the Starbucks across the library?"
# result = process_calendar_request(new_event_input5)
# if result:
#     print(f"Response: {result.message}")

# invalid_input = "What's the weather like at 4pm today?"
# result = process_calendar_request(invalid_input)
# if not result:
#     print("Request not recognized as a calendar operation")
