import re
from difflib import SequenceMatcher
from flask import Flask, jsonify, send_from_directory, request, session
from flask_cors import CORS
from flask_socketio import SocketIO, join_room, leave_room, emit
import os
import requests
import random
import string

app = Flask(__name__)
application = app
app.secret_key = os.urandom(24)  # Set a secret key for session management

# Allow CORS only for API endpoints from localhost:3000 and support credentials
CORS(app, resources={r"/api/*": {"origins": "http://liassides/me"}}, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*")

DEEZER_API_BASE_URL = 'https://api.deezer.com'
lobbies = {}
user_socket_map = {}  # Mapping from user_id to socket id

def generate_lobby_code():
    while True:
        code = ''.join(random.choices(string.digits, k=6))
        if code not in lobbies:
            return code

def clean_title(title):
    """
    Remove content in parentheses and convert to lowercase.
    For example:
    "Give Me Everything (feat. Nayer)" -> "give me everything"
    """
    # Remove any text in parentheses
    title = re.sub(r'\s*\([^)]*\)', '', title)
    return title.strip().lower()

def is_close_match(guess, correct_song, threshold=0.7):
    """
    Use fuzzy matching to determine if the guess is close enough to the correct song.
    Returns True if similarity ratio is greater than or equal to threshold.
    """
    guess_clean = clean_title(guess)
    correct_clean = clean_title(correct_song)
    ratio = SequenceMatcher(None, guess_clean, correct_clean).ratio()
    return ratio >= threshold

@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    if 'user_id' not in session:
        session['user_id'] = str(random.randint(10000, 99999))
    
    lobby_code = generate_lobby_code()
    lobbies[lobby_code] = {
        'players': [{
            'id': session['user_id'],
            'is_host': True,
            'score': 0
        }],
        'status': 'waiting',
        'round': 0,
        'max_rounds': 10,
        'current_song': None
    }
    
    print(f"Lobby created: {lobby_code}")
    return jsonify({
        'code': lobby_code,
        'is_host': True
    })


@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data = request.get_json()
    lobby_code = data.get('code')
    
    if not lobby_code or lobby_code not in lobbies:
        return jsonify({'error': 'Invalid lobby code'}), 404
    
    if 'user_id' not in session:
        session['user_id'] = str(random.randint(10000, 99999))
    
    lobby = lobbies[lobby_code]
    
    # If the user is already in the lobby, return success
    if any(p['id'] == session['user_id'] for p in lobby['players']):
        return jsonify({
            'code': lobby_code,
            'is_host': any(p['id'] == session['user_id'] and p['is_host'] for p in lobby['players'])
        })
    
    # Check if lobby is full (max 8 players)
    if len(lobby['players']) >= 8:
        return jsonify({'error': 'Lobby is full'}), 400
    
    # Add player to lobby
    lobby['players'].append({
        'id': session['user_id'],
        'is_host': False,
        'score': 0
    })
    
    # Notify other players
    socketio.emit('player_joined', {
        'player_id': session['user_id']
    }, room=lobby_code)
    
    print(f"User {session['user_id']} joined lobby {lobby_code}")
    return jsonify({
        'code': lobby_code,
        'is_host': False
    })

@app.route('/api/lobby/leave', methods=['POST'])
def leave_lobby():
    data = request.get_json()
    lobby_code = data.get('code')
    
    if not lobby_code or lobby_code not in lobbies:
        return jsonify({'error': 'Invalid lobby code'}), 404
    
    if 'user_id' not in session:
        return jsonify({'error': 'Not in a lobby'}), 400
    
    lobby = lobbies[lobby_code]
    player_id = session['user_id']
    
    # Remove player from lobby
    lobby['players'] = [p for p in lobby['players'] if p['id'] != player_id]
    
    # Remove from user_socket_map if exists
    if player_id in user_socket_map:
        del user_socket_map[player_id]
    
    # If lobby is empty, delete it
    if not lobby['players']:
        del lobbies[lobby_code]
    else:
        # If host left, assign new host
        if not any(p['is_host'] for p in lobby['players']):
            lobby['players'][0]['is_host'] = True
        
        # Notify other players
        socketio.emit('player_left', {
            'player_id': player_id
        }, room=lobby_code)
    
    return jsonify({'success': True})

# ------------------ Game Rounds ------------------
def run_game(lobby_code):
    lobby = lobbies.get(lobby_code)
    if not lobby:
        return
    lobby['round'] = 1
    max_rounds = 10  # explicitly set total rounds to 10

    while lobby['round'] <= max_rounds:
        # Reset guesses for new round: ensures one try per player.
        lobby['guesses'] = {player['id']: {'guess': None, 'submitted': False} for player in lobby['players']}
        
        # Fetch a song from Deezer API (retry logic included)
        song_data = None
        retry_count = 0
        max_retries = 3
        
        while not song_data and retry_count < max_retries:
            try:
                position = random.randint(1, 100)
                response = requests.get(
                    f'{DEEZER_API_BASE_URL}/chart/0/tracks',
                    params={'limit': 1, 'index': position}
                )
                response.raise_for_status()
                track_data = response.json()['data'][0]
                
                if track_data.get('title') and track_data.get('artist', {}).get('name') and track_data.get('preview'):
                    song_data = {
                        'song': track_data['title'],
                        'artist': track_data['artist']['name'],
                        'audio_url': track_data['preview']
                    }
                else:
                    raise ValueError("Incomplete track data received")
            except Exception as e:
                print(f"Error fetching song for round {lobby['round']} (attempt {retry_count + 1}):", e)
                retry_count += 1
                socketio.sleep(1)
        
        # If no valid song is found, skip this round.
        if not song_data:
            socketio.emit('round_started', {
                'round': lobby['round'],
                'duration': 30,
                'error': 'Failed to fetch song data. Skipping round.'
            }, room=lobby_code)
            socketio.sleep(5)  # transition pause
            lobby['round'] += 1
            continue

        lobby['current_song'] = song_data
        print(f"Starting round {lobby['round']} in lobby {lobby_code}")

        
        # Emit event to start round (players have 30 seconds to submit their guess)
        socketio.emit('round_started', {
            'round': lobby['round'],
            'duration': 30,
            'song': song_data
        }, room=lobby_code)
        
        socketio.sleep(30)  # 30-second guessing period

        # Validate guesses and build a results dictionary.
        guess_results = {}
        for player in lobby['players']:
            player_guess = lobby['guesses'].get(player['id'], {}).get('guess')
            is_correct = False
            if player_guess and player_guess.lower() == song_data['song'].lower():
                is_correct = True
                player['score'] += 1
            guess_results[player['id']] = {
                'guess': player_guess,
                'correct': is_correct
            }
        
        # Instead of broadcasting all guess results, send personalized results.
        for player in lobby['players']:
            uid = player['id']
            personal_result = {
                'round': lobby['round'],
                'correct_answer': song_data['song'],
                'guess_result': guess_results.get(uid, {'guess': None, 'correct': False})
            }
            if uid in user_socket_map:
                socketio.emit('round_ended_personal', personal_result, room=user_socket_map[uid])
        
        # Broadcast a general round transition event (without detailed guess results)
        socketio.emit('round_transition', {
            'next_round': lobby['round'] + 1 if lobby['round'] < max_rounds else None,
            'duration': 5,
        }, room=lobby_code)
        socketio.sleep(5)
        
        lobby['round'] += 1

    # After all rounds, compute the winner (highest score wins)
    winner = None
    highest_score = -1
    for p in lobby['players']:
        if p['score'] > highest_score:
            highest_score = p['score']
            winner = p['id']
    socketio.emit('game_ended', {'winner': winner, 'players': lobby['players']}, room=lobby_code)

@app.route('/api/lobby/start', methods=['POST'])
def start_game():
    data = request.get_json()
    lobby_code = data.get('code')
    
    if not lobby_code or lobby_code not in lobbies:
        return jsonify({'error': 'Invalid lobby code'}), 404
    
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'error': 'User not identified'}), 400

    lobby = lobbies[lobby_code]
    
    # Only host can start the game
    if not any(p['id'] == user_id and p['is_host'] for p in lobby['players']):
        print(f"User {user_id} is not host in lobby {lobby_code}")
        return jsonify({'error': 'Only host can start the game'}), 403
    
    lobby['status'] = 'playing'
    print(f"Starting game in lobby {lobby_code} by host {user_id}")
    socketio.emit('game_started', room=lobby_code)
    socketio.start_background_task(run_game, lobby_code)
    
    return jsonify({'success': True})

@socketio.on('join')
def on_join(data):
    lobby_code = data['lobby_code']
    join_room(lobby_code)
    user_id = session.get('user_id')
    if user_id:
        user_socket_map[user_id] = request.sid
    print(f"Socket {request.sid} joined room: {lobby_code}")

@socketio.on('leave')
def on_leave(data):
    lobby_code = data['lobby_code']
    leave_room(lobby_code)
    user_id = session.get('user_id')
    if user_id and user_id in user_socket_map:
        del user_socket_map[user_id]
    print(f"Socket {request.sid} left room: {lobby_code}")

@app.route('/api/random-song')
def get_random_song():
    try:
        position = random.randint(1, 100)
        response = requests.get(
            f'{DEEZER_API_BASE_URL}/chart/0/tracks',
            params={'limit': 1, 'index': position}
        )
        response.raise_for_status()
        track_data = response.json()['data'][0]
        return jsonify({
            'song': track_data['title'],
            'artist': track_data['artist']['name'],
            'audio_url': track_data['preview']
        })
    except requests.exceptions.RequestException as e:
        print("Error fetching song:", e)
        return jsonify({'error': 'Failed to fetch song from Deezer API'}), 500

# ------------------ Validate Guess ------------------
@app.route('/api/validate-guess', methods=['POST'])
def validate_guess():
    data = request.get_json()
    guess = data.get('guess')
    correct_song = data.get('song')

    if not guess or not correct_song:
        return jsonify({'error': 'Missing guess or song data'}), 400

    is_correct = is_close_match(guess, correct_song)

    if 'score' not in session:
        session['score'] = 0
    if is_correct:
        session['score'] += 1
    current_score = session['score']

    if session.get('user_id'):
        print(f"User {session['user_id']} guessed '{guess}' for song '{correct_song}'. Correct: {is_correct}")
    else:
        print(f"Anonymous user guessed '{guess}' for song '{correct_song}'. Correct: {is_correct}")

    return jsonify({
        'correct': is_correct,
        'message': 'Correct! 🎉' if is_correct else 'Try again! 🤔',
        'score': current_score
    })

@app.route('/api/reset-score', methods=['POST'])
def reset_score():
    session['score'] = 0
    return jsonify({'success': True, 'score': 0})

@app.route('/api/audio/<filename>')
def serve_audio(filename):
    return send_from_directory('songs', filename)

@app.route('/api/submit-guess', methods=['POST'])
def submit_guess():
    data = request.get_json()
    guess = data.get('guess')
    lobby_code = data.get('lobby_code')
    
    if not guess or not lobby_code or lobby_code not in lobbies:
        return jsonify({'error': 'Invalid request'}), 400
    
    lobby = lobbies[lobby_code]
    user_id = session.get('user_id')
    
    if lobby['guesses'].get(user_id, {}).get('submitted'):
        return jsonify({'error': 'You have already made a guess this round'}), 400
    
    lobby['guesses'][user_id] = {
        'guess': guess,
        'submitted': True
    }
    
    socketio.emit('player_guessed', {
        'player_id': user_id
    }, room=lobby_code)
    
    return jsonify({
        'success': True,
        'message': 'Guess submitted! Wait for round end to see results.'
    })


@app.route('/api/init-session', methods=['GET'])
def init_session():
    if 'user_id' not in session:
        session['user_id'] = str(random.randint(10000, 99999))
    return jsonify({'message': 'Session initialized', 'user_id': session['user_id']})

@app.route('/api/lobby/<lobby_code>', methods=['GET'])
def get_lobby(lobby_code):
    if lobby_code not in lobbies:
        return jsonify({'error': 'Lobby not found'}), 404
    return jsonify(lobbies[lobby_code])

if __name__ == '__main__':
    socketio.run(app, debug=True, use_reloader=False, port=8000)
