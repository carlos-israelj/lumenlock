from django.shortcuts import render, redirect
from django.http import JsonResponse
from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network
from stellar_sdk.exceptions import NotFoundError, BadRequestError
from stellar_sdk.client.requests_client import RequestsClient
from .models import Wallet
import cryptocode
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import requests
import json
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Configure Horizon client with timeout to prevent hanging requests
def get_horizon_server():
    """Create a Horizon Server instance with proper timeout configuration"""
    # Use RequestsClient with explicit timeout (10 seconds for API calls)
    # request_timeout: timeout for GET requests, post_timeout: timeout for POST requests
    client = RequestsClient(request_timeout=10, post_timeout=10)
    return Server(horizon_url=settings.STELLAR_HORIZON_URL, client=client)

def home(request):
    return render(request, 'home.html')

@login_required
def create_wallet(request):
    if Wallet.objects.filter(user=request.user).exists():
        return redirect('dashboard')

    keypair = Keypair.random()
    encryption_key = request.POST.get('password')

    if not encryption_key:
        return JsonResponse({'error': 'Password is required'}, status=400)

    encrypted_secret_seed = cryptocode.encrypt(keypair.secret, encryption_key)

    # Fixed: Use request.user instead of User.objects.first()
    wallet = Wallet.objects.create(
        user=request.user,
        public_key=keypair.public_key,
        secret_seed=encrypted_secret_seed
    )

    # Fund the account using Stellar's friendbot (testnet only)
    try:
        response = requests.get(settings.STELLAR_FRIENDBOT_URL, params={"addr": keypair.public_key}, timeout=10)
        response.raise_for_status()
    except requests.RequestException:
        pass  # Friendbot errors are non-critical

    return redirect('dashboard')


@login_required  # Fixed: Added missing authentication decorator
def check_balance(request):
    # Security: Only allow users to check their own wallet balance
    try:
        wallet = Wallet.objects.get(user=request.user)
        public_key = wallet.public_key
    except Wallet.DoesNotExist:
        return JsonResponse({'error': 'No wallet found for user'}, status=404)

    try:
        server = get_horizon_server()
        account = server.accounts().account_id(public_key).call()

        # Fixed: Find native XLM balance specifically, not just balances[0]
        xlm_balance = '0'
        for balance in account['balances']:
            if balance.get('asset_type') == 'native':
                xlm_balance = balance['balance']
                break

        return JsonResponse({'balance': xlm_balance})
    except NotFoundError:
        return JsonResponse({'error': 'Account not found on Stellar network'}, status=404)
    except Exception as e:
        logger.error(f"Error checking balance for {public_key}: {str(e)}", exc_info=True)
        return JsonResponse({'error': 'Unable to retrieve balance. Please try again later.'}, status=500)


@login_required
def send_money(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST method required'}, status=405)

    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    destination_public_key = data.get('recipient')
    amount = data.get('amount')
    encryption_key = data.get('transaction_password')
    memo_text = data.get('memo', '')  # New: Optional memo field

    # Validation
    if not all([destination_public_key, amount, encryption_key]):
        return JsonResponse({'error': 'Missing required fields'}, status=400)

    # Validate and normalize memo
    if memo_text:
        # Ensure memo is a string
        if not isinstance(memo_text, str):
            return JsonResponse({'error': 'Memo must be a string'}, status=400)

        # Stellar text memo limit is 28 bytes
        memo_text = memo_text.strip()
        if len(memo_text.encode('utf-8')) > 28:
            return JsonResponse({'error': 'Memo cannot exceed 28 bytes'}, status=400)

        # Validate characters (printable ASCII for safety)
        if not all(32 <= ord(c) <= 126 for c in memo_text):
            return JsonResponse({'error': 'Memo contains invalid characters. Use only printable ASCII.'}, status=400)

    # Validate amount using Decimal for precision
    try:
        amount_decimal = Decimal(str(amount))

        # Check for non-finite values
        if not amount_decimal.is_finite():
            return JsonResponse({'error': 'Amount must be a finite number'}, status=400)

        # Stellar supports up to 7 decimal places
        if amount_decimal.as_tuple().exponent < -7:
            return JsonResponse({'error': 'Amount cannot have more than 7 decimal places'}, status=400)

        if amount_decimal <= 0:
            return JsonResponse({'error': 'Amount must be positive'}, status=400)

        # Normalize to fixed-point string (prevent scientific notation like 1E+3)
        # Quantize to 7 decimal places (Stellar standard)
        amount_decimal = amount_decimal.quantize(Decimal('0.0000001'))
        amount = str(amount_decimal)
    except (ValueError, TypeError, InvalidOperation):
        return JsonResponse({'error': 'Invalid amount format'}, status=400)

    try:
        wallet = Wallet.objects.get(user=request.user)
    except Wallet.DoesNotExist:
        return JsonResponse({'error': 'No wallet found'}, status=404)

    # Decrypt secret key
    decrypted_secret = cryptocode.decrypt(wallet.secret_seed, encryption_key)
    if not decrypted_secret:
        return JsonResponse({'error': 'Invalid password'}, status=401)

    try:
        server = get_horizon_server()

        # Validate keypair creation
        try:
            source_keypair = Keypair.from_secret(decrypted_secret)
        except Exception:
            return JsonResponse({'error': 'Invalid secret key format'}, status=400)

        # Check destination account exists
        try:
            server.load_account(destination_public_key)
        except NotFoundError:
            return JsonResponse({'error': 'Invalid address: Destination account not found on network'}, status=404)
        except Exception:
            return JsonResponse({'error': 'Invalid address: Unable to verify destination account'}, status=400)

        # Load source account and check balance
        try:
            source_account = server.load_account(source_keypair.public_key)
        except NotFoundError:
            return JsonResponse({'error': 'Source account not found. Please ensure your wallet is funded.'}, status=404)

        # Check if account has sufficient balance
        xlm_balance = Decimal('0')
        for balance in source_account.balances:
            if balance.get('asset_type') == 'native':
                xlm_balance = Decimal(balance['balance'])
                break

        # Fetch current network base fee and base reserve dynamically
        try:
            # Get current fee stats from Horizon
            fee_stats = server.fee_stats().call()
            # Use max fee for reliability (fee_charged is in stroops)
            base_fee_stroops = int(fee_stats['max_fee']['max'])
            # Convert stroops to XLM (1 XLM = 10,000,000 stroops)
            transaction_fee = Decimal(str(base_fee_stroops)) / Decimal('10000000')

            # Get base reserve from ledger (in stroops)
            ledger = server.ledgers().order(desc=True).limit(1).call()
            base_reserve_stroops = int(ledger['_embedded']['records'][0]['base_reserve_in_stroops'])
            base_reserve = Decimal(str(base_reserve_stroops)) / Decimal('10000000')
        except Exception:
            # Fallback to safe defaults if API fails
            base_fee_stroops = 100
            transaction_fee = Decimal('0.00001')
            base_reserve = Decimal('0.5')

        # Calculate minimum balance required (base reserve + fee)
        # Base reserve: 1 base reserve per entry (2 entries minimum)
        # Additional reserves for trustlines, offers, signers, etc.
        num_subentries = int(source_account.subentry_count)
        min_balance = (2 + num_subentries) * base_reserve

        # Total required: amount + min_balance + fee
        total_required = amount_decimal + min_balance + transaction_fee

        if xlm_balance < total_required:
            available = xlm_balance - min_balance - transaction_fee
            return JsonResponse({
                'error': f'Insufficient funds: You need {total_required} XLM (including {min_balance} XLM minimum balance + {transaction_fee} XLM fee), but your balance is {xlm_balance} XLM. Available to send: {max(available, Decimal("0"))} XLM'
            }, status=400)

        # Build transaction with dynamic base fee
        transaction_builder = TransactionBuilder(
            source_account=source_account,
            network_passphrase=settings.STELLAR_NETWORK_PASSPHRASE,
            base_fee=base_fee_stroops
        ).append_payment_op(
            destination=destination_public_key,
            amount=amount,
            asset=Asset.native()
        )

        # Add memo if provided (already validated above)
        if memo_text:
            transaction_builder.add_text_memo(memo_text)

        transaction = transaction_builder.set_timeout(30).build()
        transaction.sign(source_keypair)

        response = server.submit_transaction(transaction)

        return JsonResponse({
            'message': 'Payment sent successfully',
            'status': 'success',
            'hash': response.get('hash', '')
        })

    except BadRequestError as e:
        error_msg = str(e)
        # Parse common Stellar errors for user-friendly messages
        if 'op_underfunded' in error_msg.lower():
            return JsonResponse({'error': 'Insufficient funds: Account does not have enough XLM for this transaction'}, status=400)
        elif 'op_no_destination' in error_msg.lower():
            return JsonResponse({'error': 'Invalid address: Destination account does not exist'}, status=400)
        elif 'tx_bad_seq' in error_msg.lower():
            return JsonResponse({'error': 'Transaction sequence error. Please try again.'}, status=400)
        else:
            return JsonResponse({'error': f'Transaction failed: {error_msg}'}, status=400)
    except Exception as e:
        logger.error(f"Unexpected error in send_money for user {request.user.id}: {str(e)}", exc_info=True)
        return JsonResponse({'error': 'An unexpected error occurred. Please try again later.'}, status=500)


@login_required
def transaction_history(request):
    """New feature: Get transaction history for user's wallet"""
    try:
        wallet = Wallet.objects.get(user=request.user)
    except Wallet.DoesNotExist:
        return JsonResponse({'error': 'No wallet found'}, status=404)

    try:
        server = get_horizon_server()

        # Fetch payments for this account (sorted newest first)
        payments = server.payments().for_account(wallet.public_key).order(desc=True).limit(50).call()

        transactions = []
        # Include all payment-related types for complete transaction history
        payment_types = {
            'payment', 'create_account',
            'path_payment_strict_send', 'path_payment_strict_receive',
            'account_merge'  # Account merge transfers all XLM
        }

        for payment in payments['_embedded']['records']:
            if payment['type'] in payment_types:
                # Handle different amount field names based on payment type
                amount = '0'
                if payment['type'] == 'create_account':
                    amount = payment.get('starting_balance', '0')
                elif payment['type'] in ('path_payment_strict_send', 'path_payment_strict_receive'):
                    # Path payments have both source and destination amounts
                    amount = payment.get('amount', payment.get('source_amount', '0'))
                else:
                    amount = payment.get('amount', '0')

                tx_data = {
                    'id': payment.get('id', ''),
                    'type': payment['type'],
                    'created_at': payment.get('created_at', ''),
                    'transaction_hash': payment.get('transaction_hash', ''),
                    'amount': amount,
                    'asset_type': payment.get('asset_type', 'native'),
                    'asset_code': payment.get('asset_code', ''),  # For non-native assets
                    'from': payment.get('from', payment.get('source_account', '')),
                    'to': payment.get('to', payment.get('account', payment.get('into', ''))),
                }
                transactions.append(tx_data)

        return JsonResponse({'transactions': transactions})

    except Exception as e:
        logger.error(f"Error fetching transaction history for wallet {wallet.public_key}: {str(e)}", exc_info=True)
        return JsonResponse({'error': 'Unable to load transaction history. Please try again later.'}, status=500)


@login_required
def dashboard(request):
    # Fetch wallet in one query to avoid race condition
    try:
        wallet = Wallet.objects.get(user=request.user)
        wallet_exists = True
    except Wallet.DoesNotExist:
        return render(request, 'dashboard.html', {'wallet_exists': False})

    try:
        server = get_horizon_server()
        account = server.accounts().account_id(wallet.public_key).call()

        # Fixed: Find native XLM balance specifically
        balance = '0'
        for bal in account['balances']:
            if bal.get('asset_type') == 'native':
                balance = bal['balance']
                break

        # Determine network type for explorer links (testnet vs mainnet)
        is_testnet = 'testnet' in settings.STELLAR_HORIZON_URL.lower()
        explorer_network = 'testnet' if is_testnet else 'public'

        context = {
            'wallet_exists': wallet_exists,
            'balance': balance,
            'public_key': wallet.public_key,
            'explorer_network': explorer_network
        }
    except Exception as e:
        logger.error(f"Error loading dashboard for user {request.user.id}: {str(e)}", exc_info=True)

        # Determine network type for explorer links even on error
        is_testnet = 'testnet' in settings.STELLAR_HORIZON_URL.lower()
        explorer_network = 'testnet' if is_testnet else 'public'

        context = {
            'wallet_exists': wallet_exists,
            'balance': '0',
            'public_key': wallet.public_key,
            'explorer_network': explorer_network,
            'error': 'Unable to load balance. Please try again later.'
        }

    return render(request, 'dashboard.html', context)
