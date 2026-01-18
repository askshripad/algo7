"""
Multi-Broker Token Generator
This script helps you generate access tokens for both Fyers and Angel Smart API
"""
import os
import webbrowser
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Fyers Configuration
FYERS_CLIENT_ID = os.getenv('FYERS_CLIENT_ID') or os.getenv('CLIENT_ID')
FYERS_SECRET_KEY = os.getenv('FYERS_SECRET_KEY') or os.getenv('SECRET_KEY')
REDIRECT_URI = "http://127.0.0.1:5000"  # Default redirect URI

# Angel Smart API Configuration
ANGEL_API_KEY = os.getenv('ANGEL_API_KEY')
ANGEL_CLIENT_CODE = os.getenv('ANGEL_CLIENT_CODE')
ANGEL_MPIN = os.getenv('ANGEL_MPIN') or os.getenv('ANGEL_PASSWORD')  # Support both for backward compatibility
ANGEL_TOTP_SECRET = os.getenv('ANGEL_TOTP_SECRET')

# Broker selection
BROKER = os.getenv('BROKER', 'fyers').lower()

def generate_fyers_token():
    """Generate a new Fyers API access token"""
    
    print("="*80)
    print("🔐 FYERS API TOKEN GENERATOR")
    print("="*80)
    print()
    
    # Check if credentials are set
    if not FYERS_CLIENT_ID:
        print("❌ ERROR: FYERS_CLIENT_ID or CLIENT_ID not found in .env file")
        print("Please add FYERS_CLIENT_ID to your .env file")
        return
    
    if not FYERS_SECRET_KEY:
        print("❌ ERROR: FYERS_SECRET_KEY or SECRET_KEY not found in .env file")
        print("Please add FYERS_SECRET_KEY to your .env file")
        return
    
    try:
        from fyers_apiv3 import fyersModel
    except ImportError:
        print("❌ ERROR: fyers-apiv3 package not installed")
        print("Please install it with: pip install fyers-apiv3")
        return
    
    print(f"✓ CLIENT_ID found: {FYERS_CLIENT_ID}")
    print(f"✓ SECRET_KEY found: {'*' * (len(FYERS_SECRET_KEY) - 4) + FYERS_SECRET_KEY[-4:]}")
    print()
    
    # Step 1: Generate auth code URL
    print("Step 1: Generating authentication URL...")
    session = fyersModel.SessionModel(
        client_id=FYERS_CLIENT_ID,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
        secret_key=FYERS_SECRET_KEY
    )
    
    auth_url = session.generate_authcode()
    print(f"✓ Auth URL generated")
    print()
    
    # Step 2: Open browser for authentication
    print("Step 2: Opening browser for authentication...")
    print("Please login and authorize the application.")
    print()
    print(f"Auth URL: {auth_url}")
    print()
    
    try:
        webbrowser.open(auth_url)
        print("✓ Browser opened. Please complete the authentication.")
    except Exception as e:
        print(f"⚠ Could not open browser automatically: {e}")
        print(f"Please manually open this URL: {auth_url}")
    
    print()
    print("="*80)
    print("INSTRUCTIONS:")
    print("="*80)
    print("1. Login to your Fyers account in the browser")
    print("2. Authorize the application")
    print("3. You will be redirected to a URL like:")
    print("   http://127.0.0.1:5000/?auth_code=XXXXX&state=XXXXX")
    print("4. Copy the 'auth_code' value from the URL")
    print("5. Paste it below when prompted")
    print("="*80)
    print()
    
    # Step 3: Get auth code from user
    auth_code = input("Enter the auth_code from the redirect URL: ").strip()
    
    if not auth_code:
        print("❌ No auth code provided. Exiting...")
        return
    
    print()
    print("Step 3: Exchanging auth code for access token...")
    
    # Step 4: Exchange auth code for access token
    try:
        session.set_token(auth_code)
        response = session.generate_token()
        
        if response.get('s') == 'ok':
            access_token = response['access_token']
            print()
            print("="*80)
            print("✅ TOKEN GENERATED SUCCESSFULLY!")
            print("="*80)
            print()
            print("Your new access token:")
            print(f"{access_token}")
            print()
            print("="*80)
            print("NEXT STEPS:")
            print("="*80)
            print("1. Open your .env file")
            print("2. Update the FYERS_ACCESS_TOKEN line with the token above:")
            print(f"   FYERS_ACCESS_TOKEN={access_token}")
            print()
            print("OR use this format if your token includes CLIENT_ID prefix:")
            print(f"   FYERS_ACCESS_TOKEN={FYERS_CLIENT_ID}:{access_token}")
            print()
            print("3. Save the .env file")
            print("4. Run your script again: python nifty_options_algo.py")
            print("="*80)
            
            # Optionally update .env file automatically
            update_env = input("\nWould you like to update .env file automatically? (y/n): ").strip().lower()
            if update_env == 'y':
                update_fyers_env_file(access_token)
        else:
            print(f"❌ Token generation failed: {response.get('message', 'Unknown error')}")
            print(f"Error details: {response}")
            
    except Exception as e:
        print(f"❌ Error generating token: {e}")
        import traceback
        traceback.print_exc()

def generate_angel_token():
    """Generate Angel Smart API session token"""
    
    print("="*80)
    print("🔐 ANGEL SMART API TOKEN GENERATOR")
    print("="*80)
    print()
    
    # Check if credentials are set
    if not ANGEL_API_KEY:
        print("❌ ERROR: ANGEL_API_KEY not found in .env file")
        print("Please add ANGEL_API_KEY to your .env file")
        return
    
    if not ANGEL_CLIENT_CODE:
        print("❌ ERROR: ANGEL_CLIENT_CODE not found in .env file")
        print("Please add ANGEL_CLIENT_CODE to your .env file")
        return
    
    if not ANGEL_MPIN:
        print("❌ ERROR: ANGEL_MPIN not found in .env file")
        print("Please add ANGEL_MPIN to your .env file")
        print()
        print("NOTE: Angel One now requires MPIN (Mobile PIN) instead of password")
        print("Get your MPIN from your Angel One account settings")
        return
    
    if not ANGEL_TOTP_SECRET:
        print("❌ ERROR: ANGEL_TOTP_SECRET not found in .env file")
        print("Please add ANGEL_TOTP_SECRET to your .env file")
        print()
        print("To get your TOTP Secret:")
        print("1. Go to https://smartapi.angelbroking.com/")
        print("2. Enable TOTP in your account settings")
        print("3. Copy the TOTP secret key")
        return
    
    try:
        from SmartApi import SmartConnect
        import pyotp
    except ImportError:
        print("❌ ERROR: smartapi-python or pyotp package not installed")
        print("Please install them with: pip install smartapi-python pyotp")
        print()
        print("If packages are installed but still getting this error:")
        print("1. Make sure you're using the same Python interpreter")
        print("2. Try: python -m pip install smartapi-python pyotp")
        return
    
    print(f"✓ API_KEY found: {ANGEL_API_KEY[:8]}...")
    print(f"✓ CLIENT_CODE found: {ANGEL_CLIENT_CODE}")
    print(f"✓ MPIN found: {'*' * len(ANGEL_MPIN)}")
    print(f"✓ TOTP_SECRET found: {'*' * (len(ANGEL_TOTP_SECRET) - 4) + ANGEL_TOTP_SECRET[-4:]}")
    print()
    
    # Step 1: Initialize SmartConnect
    print("Step 1: Initializing Smart API connection...")
    try:
        smart_api = SmartConnect(api_key=ANGEL_API_KEY)
        print("✓ Smart API initialized")
    except Exception as e:
        print(f"❌ Error initializing Smart API: {e}")
        return
    
    # Step 2: Generate TOTP
    print()
    print("Step 2: Generating TOTP code...")
    current_totp_code = None  # Initialize variable
    try:
        # Clean the TOTP secret (remove spaces, convert to uppercase)
        clean_totp_secret = ANGEL_TOTP_SECRET.strip().replace(' ', '').replace('-', '').upper()
        
        # Show what we're working with (for debugging)
        print(f"   Secret length: {len(clean_totp_secret)} characters")
        print(f"   First 10 chars: {clean_totp_secret[:10]}...")
        
        # Validate base32 format
        import base64
        import re
        
        # Check if it contains only valid base32 characters
        if not re.match(r'^[A-Z2-7]+$', clean_totp_secret):
            invalid_chars = set(clean_totp_secret) - set('ABCDEFGHIJKLMNOPQRSTUVWXYZ234567')
            print("❌ ERROR: Invalid TOTP secret format")
            print(f"   Found invalid characters: {invalid_chars}")
            print("   TOTP secret must contain ONLY: A-Z (uppercase) and 2-7 (numbers)")
            print()
            print("   Your secret contains:")
            for char in sorted(set(clean_totp_secret)):
                if char not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567':
                    print(f"     - '{char}' (INVALID - remove this)")
            print()
            print("="*80)
            print("HOW TO FIX:")
            print("="*80)
            print("1. Check your .env file - make sure ANGEL_TOTP_SECRET has no invalid characters")
            print("2. Remove any: 0, 1, 8, 9, spaces, dashes, or special characters")
            print("3. Only keep: A-Z and 2-7")
            print("4. Example of valid secret: JBSWY3DPEHPK3PXP")
            print()
            print("HOW TO GET YOUR TOTP SECRET:")
            print("="*80)
            print("1. Go to: https://smartapi.angelbroking.com/user/login")
            print("2. Login with Client ID and MPIN")
            print("3. Go to: https://smartapi.angelbroking.com/enable-totp")
            print("4. Copy the TOTP Secret Key (text string, not the QR code)")
            print("5. It should look like: JBSWY3DPEHPK3PXP")
            print("6. Add to .env: ANGEL_TOTP_SECRET=JBSWY3DPEHPK3PXP")
            print("="*80)
            return
        
        # Try to decode to validate it's valid base32 (but pyotp might handle padding)
        try:
            # Try with padding if needed
            padded_secret = clean_totp_secret
            # Base32 needs padding to be multiple of 8
            remainder = len(padded_secret) % 8
            if remainder != 0:
                padded_secret = padded_secret + '=' * (8 - remainder)
            base64.b32decode(padded_secret, casefold=True)
            print(f"✓ Base32 validation passed")
        except Exception as decode_error:
            print("⚠️  Warning: Base32 decode check failed, but trying pyotp anyway...")
            print(f"   (pyotp might handle this automatically)")
            print()
        
        # Try to generate TOTP - pyotp is more lenient with padding
        try:
            totp_obj = pyotp.TOTP(clean_totp_secret)
            current_totp_code = totp_obj.now()  # Get the 6-digit code string
            print(f"OK: TOTP code generated: {current_totp_code}")
            print("   (This code is valid for 30 seconds)")
            print(f"OK: Secret accepted ({len(clean_totp_secret)} characters)")
        except Exception as pyotp_error:
            print("❌ ERROR: Failed to generate TOTP code")
            print(f"   Error: {pyotp_error}")
            print()
            print("This means the secret format is still incorrect.")
            print()
            print(f"Your secret: {clean_totp_secret}")
            print(f"Length: {len(clean_totp_secret)} characters")
            print()
            print("Possible issues:")
            print("1. Secret is too short (should be at least 16 characters)")
            print("2. Secret has invalid characters")
            print("3. Secret format is incorrect")
            print()
            print("Make sure you copied the TOTP SECRET KEY, not the 6-digit TOTP code.")
            return
    except Exception as e:
        print(f"❌ Error generating TOTP: {e}")
        print("   Please check your ANGEL_TOTP_SECRET in .env file")
        print()
        print("Debug info:")
        print(f"   Secret from env: {ANGEL_TOTP_SECRET[:20]}... (first 20 chars)")
        print(f"   Secret length: {len(ANGEL_TOTP_SECRET)}")
        print()
        print("Common issues:")
        print("1. Secret contains invalid characters (only A-Z, 2-7 allowed)")
        print("2. Secret has spaces, dashes, or special characters")
        print("3. Secret is not in base32 format")
        print("4. Secret might be the 6-digit code instead of the secret key")
        print()
        print("See instructions above on how to get your TOTP secret.")
        import traceback
        traceback.print_exc()
        return
    
    # Step 3: Generate session
    print()
    print("Step 3: Generating session token...")
    
    # Safety check
    if current_totp_code is None:
        print("❌ ERROR: TOTP code not generated. Cannot proceed with session generation.")
        return
    
    try:
        # Use the 6-digit TOTP code string, not the TOTP object
        print(f"   Using TOTP code: {current_totp_code}")
        data = smart_api.generateSession(
            clientCode=ANGEL_CLIENT_CODE,
            password=ANGEL_MPIN,  # Note: parameter name is 'password' but value should be MPIN
            totp=str(current_totp_code)  # Ensure it's a string, not the TOTP object
        )
        
        if data.get('status') and data.get('data'):
            jwt_token = data['data']['jwtToken']
            feed_token = smart_api.getfeedToken()
            
            print()
            print("="*80)
            print("✅ SESSION TOKEN GENERATED SUCCESSFULLY!")
            print("="*80)
            print()
            print("Your session details:")
            print(f"JWT Token: {jwt_token[:50]}...")
            print(f"Feed Token: {feed_token[:50] if feed_token else 'N/A'}...")
            print()
            print("="*80)
            print("IMPORTANT NOTES:")
            print("="*80)
            print("⚠️  Angel Smart API tokens are session-based and generated at runtime")
            print("⚠️  You don't need to store the JWT token in .env file")
            print("⚠️  The script automatically generates tokens when you run it")
            print()
            print("Your .env file should have:")
            print("   ANGEL_API_KEY=your_api_key")
            print("   ANGEL_CLIENT_CODE=your_client_code")
            print("   ANGEL_MPIN=your_mpin")
            print("   ANGEL_TOTP_SECRET=your_totp_secret")
            print()
            print("The script will automatically:")
            print("   1. Generate TOTP code at runtime")
            print("   2. Create a session and get JWT token")
            print("   3. Use the token for API calls")
            print()
            print("✅ Your credentials are already configured correctly!")
            print("   Just run: python nifty_options_algo.py")
            print("="*80)
            
            # Test the connection
            print()
            test_connection = input("Would you like to test the connection? (y/n): ").strip().lower()
            if test_connection == 'y':
                test_angel_connection(smart_api, jwt_token)
        else:
            error_msg = data.get('message', 'Unknown error')
            print(f"❌ Session generation failed: {error_msg}")
            print(f"Error details: {data}")
            print()
            print("Common issues:")
            print("1. Check if your TOTP secret is correct")
            print("2. Verify your client code and MPIN (not password)")
            print("3. Make sure TOTP is enabled in your Angel account")
            print("4. Ensure the TOTP code hasn't expired (regenerate if needed)")
            print("5. Make sure you're using MPIN, not password (Angel One requirement)")
            
    except Exception as e:
        print(f"❌ Error generating session: {e}")
        import traceback
        traceback.print_exc()

def test_angel_connection(smart_api, jwt_token):
    """Test Angel API connection"""
    print()
    print("="*80)
    print("🧪 TESTING CONNECTION")
    print("="*80)
    try:
        profile = smart_api.getProfile(jwt_token)
        if profile.get('status') and profile.get('data'):
            user_data = profile['data']
            print("✅ Connection successful!")
            print(f"   Name: {user_data.get('name', 'N/A')}")
            print(f"   Email: {user_data.get('email', 'N/A')}")
            print(f"   Client Code: {user_data.get('clientcode', 'N/A')}")
        else:
            print(f"⚠️  Connection test returned: {profile.get('message', 'Unknown response')}")
    except Exception as e:
        print(f"❌ Connection test failed: {e}")

def update_fyers_env_file(access_token):
    """Update .env file with new Fyers access token"""
    env_file = ".env"
    
    if not os.path.exists(env_file):
        print(f"⚠ .env file not found. Creating new one...")
        with open(env_file, 'w') as f:
            if FYERS_CLIENT_ID:
                f.write(f"FYERS_CLIENT_ID={FYERS_CLIENT_ID}\n")
            if FYERS_SECRET_KEY:
                f.write(f"FYERS_SECRET_KEY={FYERS_SECRET_KEY}\n")
            f.write(f"FYERS_ACCESS_TOKEN={access_token}\n")
        print(f"✅ Created .env file with new token")
        return
    
    # Read existing .env file
    try:
        with open(env_file, 'r') as f:
            lines = f.readlines()
        
        # Update FYERS_ACCESS_TOKEN line (or ACCESS_TOKEN for backward compatibility)
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith('FYERS_ACCESS_TOKEN=') or line.startswith('ACCESS_TOKEN='):
                new_lines.append(f"FYERS_ACCESS_TOKEN={access_token}\n")
                updated = True
            else:
                new_lines.append(line)
        
        # If ACCESS_TOKEN line doesn't exist, add it
        if not updated:
            new_lines.append(f"FYERS_ACCESS_TOKEN={access_token}\n")
        
        # Write back to file
        with open(env_file, 'w') as f:
            f.writelines(new_lines)
        
        print(f"✅ Updated .env file with new Fyers access token")
        
    except Exception as e:
        print(f"❌ Error updating .env file: {e}")
        print("Please manually update the FYERS_ACCESS_TOKEN in your .env file")

def main():
    """Main function to select broker and generate token"""
    print("="*80)
    print("MULTI-BROKER TOKEN GENERATOR")
    print("="*80)
    print()
    print("Select broker to generate token for:")
    print("1. Fyers API")
    print("2. Angel Smart API")
    print()
    
    # Check BROKER from env or ask user
    if BROKER in ['fyers', 'angel']:
        print(f"Detected BROKER={BROKER} from .env file")
        choice = '1' if BROKER == 'fyers' else '2'
    else:
        choice = input("Enter choice (1 or 2): ").strip()
    
    print()
    
    if choice == '1':
        generate_fyers_token()
    elif choice == '2':
        generate_angel_token()
    else:
        print("ERROR: Invalid choice. Please run the script again and select 1 or 2.")

if __name__ == '__main__':
    main()
