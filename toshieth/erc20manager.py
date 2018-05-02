import asyncio
import logging
import time

from tornado.escape import json_encode

from toshi.log import configure_logger, log_unhandled_exceptions
from toshi.utils import parse_int
from toshi.config import config

from toshi.ethereum.mixin import EthereumMixin

from toshieth.tasks import BaseEthServiceWorker, BaseTaskHandler, manager_dispatcher

log = logging.getLogger("toshieth.erc20manager")

class ERC20UpdateHandler(EthereumMixin, BaseTaskHandler):

    @log_unhandled_exceptions(logger=log)
    async def update_token_cache(self, contract_address, *eth_addresses):

        if len(eth_addresses) == 0:
            return

        is_wildcard = contract_address == "*"

        async with self.db:
            if is_wildcard:
                tokens = await self.db.fetch("SELECT contract_address FROM tokens")
            else:
                tokens = [{'contract_address': contract_address}]

        if is_wildcard:
            if len(eth_addresses) > 1:
                # this is currently unneeded and dangerous
                raise Exception("wildcard update of token caches unsupported for multiple addresses")
            log.info("START update_token_cache(\"*\", {})".format(eth_addresses[0]))
            start_time = time.time()
            # NOTE: we don't remove this at the end on purpose
            # to avoid spamming of "*" refreshes
            should_run = await self.redis.set("bulk_token_update:{}".format(eth_addresses[0]), 1,
                                              expire=60, exist=self.redis.SET_IF_NOT_EXIST)
            if not should_run:
                log.info("ABORT update_token_cache(\"*\", {}): {}".format(eth_addresses[0], should_run))
                return

        client = self.eth.bulk()
        futures = []
        for eth_address in eth_addresses:
            for token in tokens:
                data = "0x70a08231000000000000000000000000" + eth_address[2:]
                f = client.eth_call(to_address=token['contract_address'], data=data)
                futures.append((token['contract_address'], eth_address, f))

        if len(futures) > 0:
            await client.execute()

            bulk_insert = []
            bulk_delete = []
            for contract_address, eth_address, f in futures:
                try:
                    value = f.result()
                    if value == "0x0000000000000000000000000000000000000000000000000000000000000000" or value == "0x":
                        if value == "0x":
                            log.warning("calling balanceOf for contract {} failed".format(contract_address))
                        value = 0
                    else:
                        value = parse_int(value)  # remove hex padding of value
                    if value > 0:
                        bulk_insert.append((contract_address, eth_address, hex(value)))
                    else:
                        bulk_delete.append((contract_address, eth_address))
                except:
                    log.exception("WARNING: failed to update token cache of '{}' for address: {}".format(contract_address, eth_address))

            send_update = False
            if len(bulk_insert) > 0 or len(bulk_delete) > 0:
                async with self.db:
                    if len(bulk_insert) > 0:
                        await self.db.executemany(
                            "INSERT INTO token_balances (contract_address, eth_address, value) "
                            "VALUES ($1, $2, $3) "
                            "ON CONFLICT (contract_address, eth_address) "
                            "DO UPDATE set value = EXCLUDED.value",
                            bulk_insert)
                        send_update = True
                    if len(bulk_delete) > 0:
                        rval = await self.db.executemany(
                            "DELETE FROM token_balances WHERE contract_address = $1 AND eth_address = $2",
                            bulk_delete)
                        if rval != "DELETE 0":
                            send_update = True
                    await self.db.commit()

            # wildcard updates usually mean we need to send a refresh trigger to clients
            # currently clients only use a TokenPayment as a trigger to refresh their
            # token cache, so we abuse this functionality here
            if is_wildcard and send_update:
                # lots of fake values so it doesn't get confused with a real tx
                data = {
                    "txHash": "0x0000000000000000000000000000000000000000000000000000000000000000",
                    "fromAddress": "0x0000000000000000000000000000000000000000",
                    "toAddress": eth_addresses[0],
                    "status": "confirmed",
                    "value": "0x0",
                    "contractAddress": "0x0000000000000000000000000000000000000000"
                }
                message = "SOFA::TokenPayment: " + json_encode(data)
                manager_dispatcher.send_notification(eth_addresses[0], message)
        if is_wildcard:
            end_time = time.time()
            log.info("DONE update_token_cache(\"*\", {}) in {}s".format(eth_addresses[0], round(end_time - start_time, 2)))

class TaskManager(BaseEthServiceWorker):

    def __init__(self):
        super().__init__([(ERC20UpdateHandler,)], queue_name="erc20")
        configure_logger(log)

if __name__ == "__main__":
    from toshieth.app import extra_service_config
    extra_service_config()
    app = TaskManager()
    app.work()
    asyncio.get_event_loop().run_forever()
