Yes, I am with you. Breaking the master plan into a concrete, step-by-step checklist is the correct way to proceed. This approach ensures each component is built and verified on a solid foundation before the next one is added.

Here is the detailed to-do list that will take your project from its current state to the full-minded, adaptive security framework we designed. We will follow your proposed sequence: **PQC Foundation -> DDoS Mitigation -> Adaptive Scheduler**, with benchmarking integrated throughout.

---
### ### **Implementation To-Do List: From Static Proxy to Adaptive Framework**

#### **Phase 1: Solidifying the Core PQC Proxy (The Foundation)**
**Goal:** Transform the codebase from a collection of scripts into a single, robust, and configurable command-line application with a secure key management story.

* **1.1: Unify the Application Layer**
    * `[ ]` **Delete the `drone/wrappers` and `gcs/wrappers` directories.** This is a critical step to eliminate redundancy and simplify the architecture.
    * `[ ]` **Delete the `core/runner.py` file.** Its logic is being replaced by a proper CLI.
    * `[ ]` **Enhance `core/run_proxy.py` to be the sole entrypoint.** It must accept command-line arguments for all operational parameters, including:
        * `--suite <suite_name>`
        * `--key-file /path/to/secret.key` (for the GCS)
        * `--peer-pubkey-file /path/to/public.pub` (for the drone)
        * `--role <drone|gcs>`

* **1.2: Implement Secure Key Management**
    * `[ ]` **Create a new script `tools/generate_identity.py`.** This tool's only purpose is to generate a PQC signature keypair for a given suite and save the secret/public keys to separate files. It must set secure file permissions (`0600`) on the secret key file.
    * `[ ]` **Modify `core/run_proxy.py` to load keys from files.** The GCS will load its secret key; the drone will load the GCS public key it has been provisioned with.
    * `[ ]` **Update `core/handshake.py`.** The `server_gcs_handshake` function must be modified to accept the raw `bytes` of the secret key and correctly initialize an `oqs.Signature` object from it.

* **1.3: Finalize Core Logic and Verification**
    * `[ ]` **Add strict `assert` statements to `tests/test_end_to_end_proxy.py`** to programmatically fail the test if the forwarded MAVLink data does not exactly match the original data.
    * `[ ]` **Add a new test `tests/test_handshake_downgrade.py`** to verify that your change to the signed transcript (including the version byte) correctly prevents a downgrade attack.
    * `[ ]` **Refactor exception handling** in `core` modules to use appropriate exceptions (`ValueError`, `ConnectionError`, etc.) instead of `NotImplementedError`.

**Checkpoint 1:** At the end of this phase, you will have a single, professional command-line application. You can securely generate a GCS identity, start the proxy with any supported PQC suite, and have a test suite that *proves* it works.

---
#### **Phase 2: Building the Multi-Layered Attack Mitigation**
**Goal:** Add layers of defense to handle resource exhaustion and sophisticated attacks.

* **2.1: Implement the Layer-1 Gatekeeper (DDoS Handshake Shield)**
    * `[ ]` **In `core/async_proxy.py`, implement a rate-limiter.** This should be a simple in-memory dictionary that tracks the timestamps of connection attempts from each source IP. If an IP exceeds a set threshold (e.g., 5 attempts in 10 seconds), subsequent connection attempts from it are immediately dropped *before* calling the expensive PQC handshake function.
    * `[ ]` **Create a new test file, `tests/test_gatekeeper.py`,** to verify that the rate-limiter correctly allows normal connection rates but blocks flood-like behavior.

* **2.2: Integrate the Layer-2 Inspector (ML Anomaly Detection)**
    * `[ ]` **Create a new module `core/inspector.py`.** This will house your ML models. Start with a simple placeholder function like `analyze_mavlink_packet(payload)`.
    * `[ ]` **In `core/async_proxy.py`'s main loop,** after a data packet is successfully decrypted, pass the plaintext MAVLink payload to the `analyze_mavlink_packet` function.
    * `[ ]` **Delete the top-level `ddos/` directory.** Its purpose is now integrated into the core application in a more logical, layered way.

**Checkpoint 2:** Your application is now hardened against simple handshake floods and has the necessary hooks to perform intelligent analysis on the application-layer traffic.

---
#### **Phase 3: Implementing the Adaptive Scheduling Engine**
**Goal:** Evolve the proxy from a static secure channel into a dynamic, self-reconfiguring system.

* **3.1: Implement the In-Band Control Channel**
    * `[ ]` **Modify the AEAD framing in `core/aead.py`** to reserve the first byte of the plaintext payload as a "type" flag (e.g., `0x01` for data, `0x02` for control). The `encrypt()` method will prepend it, and the `decrypt()` method will parse and return it along with the actual payload.
    * `[ ]` **Update the main loop in `core/async_proxy.py`** to check this flag. Data packets are forwarded to MAVProxy; control packets are routed to the new Policy Engine.

* **3.2: Implement the Scheduler (Policy Engine)**
    * `[ ]` **Create a new module `core/policy_engine.py`**.
    * `[ ]` **Implement a basic rule-based scheduler.** This will be a function that takes a dictionary representing the drone's state (e.g., `{'battery': 85, 'mission': 'TRANSIT'}`) and returns a target suite ID (e.g., `"cs-kyber768-aesgcm-dilithium3"`).
    * `[ ]` **The main proxy loop must periodically query the drone's state** and call the scheduler. If the desired suite changes, the engine will use the in-band control channel to initiate a re-key.

* **3.3: Implement the Dynamic Re-Handshake Mechanism**
    * `[ ]` This is the most complex task. You need to define the state machine for re-keying. When a `rekey` command is sent and acknowledged, both proxies must be able to:
        1.  Initiate a new PQC handshake.
        2.  Successfully establish a new set of session keys.
        3.  Atomically replace the old `Sender` and `Receiver` objects with new ones configured with the new keys and an incremented `epoch`.
        4.  Resume data forwarding without dropping the underlying UDP connection.

**Checkpoint 3:** You now have a fully functional adaptive security proxy. It can defend itself against attacks and intelligently reconfigure its own cryptographic protocol based on a defined policy.

---
#### **Continuous Task: Benchmarking & Data Collection**
This runs in parallel with the implementation phases.

* `[ ]` **After Phase 1:** Perform the baseline energy (V\*I\*t) and performance (`perf`) measurements for the initial handshake and steady-state data transfer for all PQC suites. **This dataset is your first major research result.**
* `[ ]` **After Phase 3:** Measure the energy and performance cost of the **dynamic re-handshake** process. This provides crucial data on the cost of adaptation itself.
* `[ ]` **Final Stage:** With the complete performance and energy profile dataset, you now have the high-quality ground truth needed to train your final RL model, which will replace the simple rule-based scheduler in the Policy Engine.