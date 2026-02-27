from django.shortcuts import render, redirect
from django.http import JsonResponse
from stellar_sdk import Asset, Server, Keypair, TransactionBuilder, Network
from stellar_sdk.exceptions import NotFoundError, BadRequestError
from .models import Wallet
import cryptocode
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
import requests
import json
from decimal import Decimal, InvalidOperation

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
    public_key = request.POST.get('public_key')

    if not public_key:
        try:
            wallet = Wallet.objects.get(user=request.user)
            public_key = wallet.public_key
        except Wallet.DoesNotExist:
            return JsonResponse({'error': 'No wallet found for user'}, status=404)

    try:
        server = Server(settings.STELLAR_HORIZON_URL)
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
        return JsonResponse({'error': str(e)}, status=500)


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

        # Convert to string for Stellar SDK (maintains precision)
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
        server = Server(settings.STELLAR_HORIZON_URL)

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

        if xlm_balance < amount_decimal:
            return JsonResponse({
                'error': f'Insufficient funds: Your balance is {xlm_balance} XLM, but you are trying to send {amount} XLM'
            }, status=400)

        # Build transaction
        transaction_builder = TransactionBuilder(
            source_account=source_account,
            network_passphrase=settings.STELLAR_NETWORK_PASSPHRASE,
            base_fee=100
        ).append_payment_op(
            destination=destination_public_key,
            amount=amount,
            asset=Asset.native()
        )

        # Add memo if provided
        if memo_text:
            from stellar_sdk import TextMemo
            transaction_builder.add_text_memo(memo_text[:28])  # Stellar limit

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
        return JsonResponse({'error': f'Unexpected error: {str(e)}'}, status=500)


@login_required
def transaction_history(request):
    """New feature: Get transaction history for user's wallet"""
    try:
        wallet = Wallet.objects.get(user=request.user)
    except Wallet.DoesNotExist:
        return JsonResponse({'error': 'No wallet found'}, status=404)

    try:
        server = Server(settings.STELLAR_HORIZON_URL)

        # Fetch payments for this account (sorted newest first)
        payments = server.payments().for_account(wallet.public_key).order(desc=True).limit(50).call()

        transactions = []
        for payment in payments['_embedded']['records']:
            if payment['type'] == 'payment' or payment['type'] == 'create_account':
                tx_data = {
                    'id': payment.get('id', ''),
                    'type': payment['type'],
                    'created_at': payment.get('created_at', ''),
                    'transaction_hash': payment.get('transaction_hash', ''),
                    'amount': payment.get('amount', payment.get('starting_balance', '0')),
                    'asset_type': payment.get('asset_type', 'native'),
                    'from': payment.get('from', ''),
                    'to': payment.get('to', payment.get('account', '')),
                }
                transactions.append(tx_data)

        return JsonResponse({'transactions': transactions})

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def dashboard(request):
    wallet_exists = Wallet.objects.filter(user=request.user).exists()

    if not wallet_exists:
        return render(request, 'dashboard.html', {'wallet_exists': wallet_exists})

    wallet = Wallet.objects.get(user=request.user)

    try:
        server = Server(settings.STELLAR_HORIZON_URL)
        account = server.accounts().account_id(wallet.public_key).call()

        # Fixed: Find native XLM balance specifically
        balance = '0'
        for bal in account['balances']:
            if bal.get('asset_type') == 'native':
                balance = bal['balance']
                break

        context = {
            'wallet_exists': wallet_exists,
            'balance': balance,
            'public_key': wallet.public_key
        }
    except Exception as e:
        context = {
            'wallet_exists': wallet_exists,
            'balance': '0',
            'public_key': wallet.public_key,
            'error': str(e)
        }

    return render(request, 'dashboard.html', context)
