# ── trader/executor.py ────────────────────────────────────────────────────────
# Executes swaps on Uniswap V3 via web3.py.
# Supports paper trading mode (logs only) and live mode (real transactions).
#
# Uniswap V3 swap path:
#   web3.py → UniversalRouter.execute() → Uniswap V3 pool → token transfer
#
# The UniversalRouter is Uniswap's current preferred swap entrypoint.
# Docs: https://docs.uniswap.org/contracts/universal-router/overview

import os
import json
import time
import logging
from datetime import datetime, timezone

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from . import config

logger = logging.getLogger(__name__)

# ── UNISWAP V3 ABI (minimal — only what we need) ─────────────────────────────
# Full ABI at: https://github.com/Uniswap/universal-router

ERC20_ABI = json.loads('''[
  {"name":"balanceOf","type":"function","stateMutability":"view",
   "inputs":[{"name":"account","type":"address"}],
   "outputs":[{"name":"","type":"uint256"}]},
  {"name":"decimals","type":"function","stateMutability":"view",
   "inputs":[],"outputs":[{"name":"","type":"uint8"}]},
  {"name":"approve","type":"function","stateMutability":"nonpayable",
   "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
   "outputs":[{"name":"","type":"bool"}]}
]''')

# Uniswap V3 SwapRouter02 ABI (exactInputSingle)
# Using SwapRouter02 (simpler than UniversalRouter for single-hop swaps)
SWAP_ROUTER_ADDRESS = '0x68b3465833fb72A70ecDF485E0e4C7bD8665Fc45'  # SwapRouter02
SWAP_ROUTER_ABI = json.loads('''[
  {
    "name": "exactInputSingle",
    "type": "function",
    "stateMutability": "payable",
    "inputs": [{
      "name": "params",
      "type": "tuple",
      "components": [
        {"name":"tokenIn",        "type":"address"},
        {"name":"tokenOut",       "type":"address"},
        {"name":"fee",            "type":"uint24"},
        {"name":"recipient",      "type":"address"},
        {"name":"amountIn",       "type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},
        {"name":"sqrtPriceLimitX96","type":"uint160"}
      ]
    }],
    "outputs":[{"name":"amountOut","type":"uint256"}]
  }
]''')

# Standard Uniswap V3 fee tiers
FEE_TIERS = [500, 3000, 10000]  # 0.05%, 0.3%, 1%


class Executor:
    """
    Handles both paper trading (dry run) and live swap execution.

    Usage:
        executor = Executor(paper=True)   # paper mode — no real txns
        executor = Executor(paper=False)  # live mode — real money

        result = executor.swap_weth_for_token(
            token_address='0x...',
            pool_fee=3000,
            weth_amount_wei=...,
        )
    """

    def __init__(self, paper: bool = True):
        self.paper = paper

        rpc_url = os.environ.get('ALCHEMY_RPC_URL')
        if not rpc_url:
            raise EnvironmentError('ALCHEMY_RPC_URL environment variable not set')

        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        # POA middleware needed for some networks; harmless on mainnet
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        if not self.w3.is_connected():
            raise ConnectionError('Could not connect to Ethereum node via Alchemy')

        if not paper:
            pk = os.environ.get('WALLET_PRIVATE_KEY')
            if not pk:
                raise EnvironmentError('WALLET_PRIVATE_KEY not set (required for live mode)')
            self.account = self.w3.eth.account.from_key(pk)
            self.router  = self.w3.eth.contract(
                address=Web3.to_checksum_address(SWAP_ROUTER_ADDRESS),
                abi=SWAP_ROUTER_ABI,
            )
            logger.info(f'Live executor — wallet: {self.account.address}')
        else:
            self.account = None
            self.router  = None
            logger.info('Paper trading mode — no real transactions will be sent')

    # ── WALLET QUERIES ────────────────────────────────────────────────────────

    def get_weth_balance(self) -> int:
        """Returns WETH balance in wei."""
        weth = self.w3.eth.contract(
            address=Web3.to_checksum_address(config.WETH_ADDRESS),
            abi=ERC20_ABI,
        )
        if self.paper:
            # In paper mode, simulate 1 WETH balance for sizing calculations
            return Web3.to_wei(1, 'ether')
        return weth.functions.balanceOf(self.account.address).call()

    def get_token_balance(self, token_address: str) -> tuple[int, int]:
        """Returns (raw_balance_wei, decimals) for a token."""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        if self.paper:
            return 0, 18
        bal      = token.functions.balanceOf(self.account.address).call()
        decimals = token.functions.decimals().call()
        return bal, decimals

    def get_current_price_onchain(self, pool_address: str) -> float | None:
        """
        Optional: could fetch slot0 from the V3 pool contract for real-time price.
        For now we use GeckoTerminal prices (already fetched) — this is a hook
        for future use if you want true onchain price verification before swapping.
        """
        return None

    # ── POSITION SIZING ───────────────────────────────────────────────────────

    def calc_position_size_wei(self) -> int:
        """
        Returns how many wei of WETH to use for a new position.
        = POSITION_SIZE_PCT × current WETH balance
        """
        balance = self.get_weth_balance()
        return int(balance * config.POSITION_SIZE_PCT)

    # ── SWAP: WETH → TOKEN (entry) ────────────────────────────────────────────

    def swap_weth_for_token(self, token_address: str, pool_fee: int,
                            weth_amount_wei: int) -> dict:
        """
        Buy token with WETH. Used for ENTRY signals.

        Returns a result dict with tx_hash (or 'PAPER' in paper mode),
        amount_in_wei, and timestamp.
        """
        weth_cs  = Web3.to_checksum_address(config.WETH_ADDRESS)
        token_cs = Web3.to_checksum_address(token_address)
        amount   = weth_amount_wei
        min_out  = int(amount * (1 - config.SLIPPAGE_TOLERANCE))  # simplified
        deadline = int(time.time()) + config.TX_DEADLINE_SECONDS

        log_msg = (f'SWAP WETH→{token_address[:8]}… '
                   f'amountIn={Web3.from_wei(amount, "ether"):.6f} WETH '
                   f'fee={pool_fee} slippage={config.SLIPPAGE_TOLERANCE*100}%')

        if self.paper:
            logger.info(f'[PAPER] {log_msg}')
            return {
                'mode':          'paper',
                'action':        'buy',
                'token_address': token_address,
                'amount_in_wei': amount,
                'pool_fee':      pool_fee,
                'tx_hash':       'PAPER',
                'timestamp':     datetime.now(timezone.utc).isoformat(),
            }

        logger.info(f'[LIVE] {log_msg}')

        # Approve router to spend WETH
        self._approve_token(weth_cs, SWAP_ROUTER_ADDRESS, amount)

        params = {
            'tokenIn':          weth_cs,
            'tokenOut':         token_cs,
            'fee':              pool_fee,
            'recipient':        self.account.address,
            'amountIn':         amount,
            'amountOutMinimum': min_out,
            'sqrtPriceLimitX96': 0,
        }

        tx   = self.router.functions.exactInputSingle(params).build_transaction({
            'from':     self.account.address,
            'gas':      300_000,
            'maxFeePerGas':         self.w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': Web3.to_wei(2, 'gwei'),
            'nonce':    self.w3.eth.get_transaction_count(self.account.address),
            'deadline': deadline,
        })
        signed  = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] != 1:
            raise RuntimeError(f'Swap tx reverted: {tx_hash.hex()}')

        logger.info(f'[LIVE] Buy confirmed: {tx_hash.hex()}')
        return {
            'mode':          'live',
            'action':        'buy',
            'token_address': token_address,
            'amount_in_wei': amount,
            'pool_fee':      pool_fee,
            'tx_hash':       tx_hash.hex(),
            'gas_used':      receipt['gasUsed'],
            'timestamp':     datetime.now(timezone.utc).isoformat(),
        }

    # ── SWAP: TOKEN → WETH (exit / stop loss) ────────────────────────────────

    def swap_token_for_weth(self, token_address: str, pool_fee: int,
                            token_amount_wei: int, reason: str = 'exit') -> dict:
        """
        Sell token back to WETH. Used for EXIT and STOP LOSS signals.
        reason: 'exit' | 'stop'
        """
        weth_cs  = Web3.to_checksum_address(config.WETH_ADDRESS)
        token_cs = Web3.to_checksum_address(token_address)
        amount   = token_amount_wei
        min_out  = int(amount * (1 - config.SLIPPAGE_TOLERANCE))
        deadline = int(time.time()) + config.TX_DEADLINE_SECONDS

        log_msg = (f'SWAP {token_address[:8]}…→WETH '
                   f'amountIn={amount} reason={reason}')

        if self.paper:
            logger.info(f'[PAPER] {log_msg}')
            return {
                'mode':          'paper',
                'action':        reason,
                'token_address': token_address,
                'amount_in_wei': amount,
                'pool_fee':      pool_fee,
                'tx_hash':       'PAPER',
                'timestamp':     datetime.now(timezone.utc).isoformat(),
            }

        logger.info(f'[LIVE] {log_msg}')

        self._approve_token(token_cs, SWAP_ROUTER_ADDRESS, amount)

        params = {
            'tokenIn':          token_cs,
            'tokenOut':         weth_cs,
            'fee':              pool_fee,
            'recipient':        self.account.address,
            'amountIn':         amount,
            'amountOutMinimum': min_out,
            'sqrtPriceLimitX96': 0,
        }

        tx   = self.router.functions.exactInputSingle(params).build_transaction({
            'from':     self.account.address,
            'gas':      300_000,
            'maxFeePerGas':         self.w3.eth.gas_price * 2,
            'maxPriorityFeePerGas': Web3.to_wei(2, 'gwei'),
            'nonce':    self.w3.eth.get_transaction_count(self.account.address),
            'deadline': deadline,
        })
        signed  = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt['status'] != 1:
            raise RuntimeError(f'Swap tx reverted: {tx_hash.hex()}')

        logger.info(f'[LIVE] {reason.capitalize()} confirmed: {tx_hash.hex()}')
        return {
            'mode':          'live',
            'action':        reason,
            'token_address': token_address,
            'amount_in_wei': amount,
            'pool_fee':      pool_fee,
            'tx_hash':       tx_hash.hex(),
            'gas_used':      receipt['gasUsed'],
            'timestamp':     datetime.now(timezone.utc).isoformat(),
        }

    # ── INTERNAL ─────────────────────────────────────────────────────────────

    def _approve_token(self, token_address: str, spender: str, amount: int):
        """Approve the router to spend tokens on our behalf."""
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        tx = token.functions.approve(
            Web3.to_checksum_address(spender), amount
        ).build_transaction({
            'from':  self.account.address,
            'nonce': self.w3.eth.get_transaction_count(self.account.address),
            'gas':   60_000,
        })
        signed  = self.w3.eth.account.sign_transaction(tx, self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        logger.info(f'Approved {token_address[:8]}… for router')
