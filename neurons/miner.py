# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 KMFODA

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import hivemind
import time
import typing
import bittensor as bt

# Bittensor Miner Template:
import template

# import base miner class which takes care of most of the boilerplate
from template.base.miner import BaseMinerNeuron

import bittensor as bt
import torch
from datasets import load_dataset
import hivemind
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import (
    AdamW,
    AutoModelForCausalLM,
    AutoTokenizer,
    get_linear_schedule_with_warmup,
    default_data_collator
)

class Miner(BaseMinerNeuron):
    def __init__(self, config=None):
        super(Miner, self).__init__(config=config)

        # TODO(developer): Anything specific to your use case you can do here

    async def forward(
        self, synapse: template.protocol.Train
    ) -> template.protocol.Train:
        """
        Processes the incoming 'Train' synapse by performing a training run

        Args:
            synapse (template.protocol.Train): The synapse object containing the 'dataset_indices' data.

        Returns:
            template.protocol.Train: The synapse object with the 'loss' field set to models loss.
        """

        # # Use CUDA if available, otherwise use CPU
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        dht = hivemind.DHT(initial_peers=[synapse.initial_peers], start=True)
        model = AutoModelForCausalLM.from_pretrained(synapse.model_name)
        opt = torch.optim.AdamW(model.parameters(), lr = synapse.lr)

        # Add a while True: loop here or sth?
        # Set up a decentralized optimizer that will average with peers in background

        opt = hivemind.Optimizer(
            dht=dht,                  # use a DHT that is connected with other peers
            run_id=synapse.run_id,    # unique identifier of this collaborative run
            batch_size_per_step=32,   # each call to opt.step adds this many samples towards the next epoch
            target_batch_size=10000,  # after peers collectively process this many samples, average weights and begin the next epoch
            optimizer=opt,            # wrap the SGD optimizer defined above
            use_local_updates=True,   # perform optimizer steps with local gradients, average parameters in background
            matchmaking_time=3.0,     # when averaging parameters, gather peers in background for up to this many seconds
            averaging_timeout=10.0,   # give up on averaging if not successful in this many seconds
            verbose=True              # print logs incessently
        )
        

        tokenizer = AutoTokenizer.from_pretrained(synapse.model_name)
        
        # Add the EOS token as PAD token to ensure our dataloader doesn't throw an error for sequences of unequal length
        tokenizer.pad_token = tokenizer.eos_token
        # Move the model to the appropriate device
        model.to(device)

        # Load dataset
        dataset = load_dataset(synapse.dataset_name, 'wikitext-2-v1', split='train')
        dataset = dataset.select(synapse.dataset_indices)
        # breakpoint()
        # Define encoding function
        def encode(examples):
            return tokenizer(examples['text'], truncation=True, max_length=512, padding='max_length', return_tensors='pt')

        # Encode the dataset
        encoded_dataset = dataset.map(encode, batched=True)
        
        # Create a PyTorch DataLoader
        dataloader = DataLoader(encoded_dataset, batch_size=synapse.batch_size, collate_fn=default_data_collator)

        # Train data for one epoch
        for step, batch in enumerate(dataloader):
            
            input_ids = batch['input_ids'].to(device)
            attention_mask = batch['attention_mask'].to(device)
            labels = input_ids.clone()

            opt.zero_grad()
            
            # Forward pass
            outputs = model(
                input_ids = input_ids, 
                attention_mask = attention_mask,
                labels = labels
            )     
            
            # Backward pass    
            loss = outputs.loss
            loss.backward()
            # Adjust gradient
            opt.step()
            
        synapse.loss = loss

        bt.logging.info(f"loss {synapse.loss}")
        # breakpoint()
        return synapse


    async def blacklist(
        self, synapse: template.protocol.Train
    ) -> typing.Tuple[bool, str]:
        """
        Determines whether an incoming request should be blacklisted and thus ignored. Your implementation should
        define the logic for blacklisting requests based on your needs and desired security parameters.

        Blacklist runs before the synapse data has been deserialized (i.e. before synapse.data is available).
        The synapse is instead contructed via the headers of the request. It is important to blacklist
        requests before they are deserialized to avoid wasting resources on requests that will be ignored.

        Args:
            synapse (template.protocol.Train): A synapse object constructed from the headers of the incoming request.

        Returns:
            Tuple[bool, str]: A tuple containing a boolean indicating whether the synapse's hotkey is blacklisted,
                            and a string providing the reason for the decision.

        This function is a security measure to prevent resource wastage on undesired requests. It should be enhanced
        to include checks against the metagraph for entity registration, validator status, and sufficient stake
        before deserialization of synapse data to minimize processing overhead.

        Example blacklist logic:
        - Reject if the hotkey is not a registered entity within the metagraph.
        - Consider blacklisting entities that are not validators or have insufficient stake.

        In practice it would be wise to blacklist requests from entities that are not validators, or do not have
        enough stake. This can be checked via metagraph.S and metagraph.validator_permit. You can always attain
        the uid of the sender via a metagraph.hotkeys.index( synapse.dendrite.hotkey ) call.

        Otherwise, allow the request to be processed further.
        """
        if synapse.dendrite.hotkey not in self.metagraph.hotkeys:
            # Ignore requests from unrecognized entities.
            bt.logging.trace(
                f"Blacklisting unrecognized hotkey {synapse.dendrite.hotkey}"
            )
            return True, "Unrecognized hotkey"

        bt.logging.trace(
            f"Not Blacklisting recognized hotkey {synapse.dendrite.hotkey}"
        )
        return False, "Hotkey recognized!"

    async def priority(self, synapse: template.protocol.Train) -> float:
        """
        The priority function determines the order in which requests are handled. More valuable or higher-priority
        requests are processed before others. You should design your own priority mechanism with care.

        This implementation assigns priority to incoming requests based on the calling entity's stake in the metagraph.

        Args:
            synapse (template.protocol.Train): The synapse object that contains metadata about the incoming request.

        Returns:
            float: A priority score derived from the stake of the calling entity.

        Miners may recieve messages from multiple entities at once. This function determines which request should be
        processed first. Higher values indicate that the request should be processed first. Lower values indicate
        that the request should be processed later.

        Example priority logic:
        - A higher stake results in a higher priority value.
        """
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        )  # Get the caller index.
        prirority = float(
            self.metagraph.S[caller_uid]
        )  # Return the stake as the priority.
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority


# This is the main function, which runs the miner.
if __name__ == "__main__":
    with Miner() as miner:
        while True:
            bt.logging.info("Miner running...", time.time())
            time.sleep(5)
