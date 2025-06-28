import os
import subprocess
import signal
import sys
import shutil
import gradio as gr
import tempfile
from huggingface_hub import HfApi, ModelCard, whoami
from gradio_huggingfacehub_search import HuggingfaceHubSearch
from pathlib import Path
from textwrap import dedent
from apscheduler.schedulers.background import BackgroundScheduler

# --- CONFIGURATION & CONSTANTS ---
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"
HF_TOKEN = os.environ.get("HF_TOKEN")
CONVERSION_SCRIPT = "./llama.cpp/convert_hf_to_gguf.py"

# --- HELPER FUNCTIONS ---

def escape_html(s: str) -> str:
    # Escapes a string for safe HTML rendering.
    s = str(s)
    s = s.replace("&", "&amp;") # Must be done first!
    s = s.replace("<", "&lt;")
    s = s.replace(">", "&gt;")
    s = s.replace('"', "&quot;")
    s = s.replace("\n", "<br/>")
    return s

def get_platform_executable(base_name: str) -> str:
    # Returns the platform-specific executable name and path.
    executable = f"{base_name}.exe" if sys.platform == "win32" else base_name
    return os.path.join(".", "llama.cpp", executable)

def generate_importance_matrix(model_path: str, train_data_path: str, output_path: str):
    # Generates the importance matrix using llama-imatrix.
    imatrix_executable = get_platform_executable("llama-imatrix")
    imatrix_command = [imatrix_executable, "-m", model_path, "-f", train_data_path, "-o", output_path, "-ngl", "0"]
    
    # --- START OF DLL FIX ---
    # Temporarily rename the problematic RPC DLL to prevent it from being loaded.
    dll_path = os.path.join(".", "llama.cpp", "ggml-rpc.dll")
    hidden_dll_path = os.path.join(".", "llama.cpp", "ggml-rpc.dll.hidden")
    
    rpc_dll_exists = os.path.exists(dll_path)
    
    try:
        if rpc_dll_exists:
            print(f"Temporarily hiding {dll_path} to force CPU backend...")
            os.rename(dll_path, hidden_dll_path)

        print("Running imatrix command...")
        process = subprocess.run(imatrix_command, capture_output=True, text=True)
        if process.returncode != 0:
            # Re-raise the exception with stdout and stderr for better debugging
            raise Exception(f"Imatrix generation failed:\nSTDOUT:\n{process.stdout}\n\nSTDERR:\n{process.stderr}")
        print("Importance matrix generation completed.")

    finally:
        # CRITICAL: Always rename the DLL back, even if the process fails.
        if rpc_dll_exists:
            print(f"Restoring {dll_path}...")
            os.rename(hidden_dll_path, dll_path)
    # --- END OF DLL FIX ---

def split_and_upload_shards(model_path: str, outdir: str, repo_id: str, oauth_token: str, split_max_tensors=256, split_max_size=None):
    # Splits a GGUF model and uploads the shards.
    split_executable = get_platform_executable("llama-gguf-split")
    model_path_prefix = '.'.join(model_path.split('.')[:-1])
    
    split_cmd = [split_executable, "--split"]
    if split_max_size:
        split_cmd.extend(["--split-max-size", split_max_size])
    else:
        split_cmd.extend(["--split-max-tensors", str(split_max_tensors)])
    split_cmd.extend([model_path, model_path_prefix])

    print(f"Running split command: {split_cmd}")
    result = subprocess.run(split_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise Exception(f"Error splitting the model: {result.stderr}")
    print("Model split successfully!")

    if os.path.exists(model_path):
        os.remove(model_path)

    model_file_prefix = os.path.basename(model_path_prefix)
    sharded_files = [f for f in os.listdir(outdir) if f.startswith(model_file_prefix) and f.endswith(".gguf")]
    if not sharded_files:
        raise Exception("No sharded files found after splitting.")

    api = HfApi(token=oauth_token)
    for file in sharded_files:
        file_path = os.path.join(outdir, file)
        print(f"Uploading shard: {file_path}")
        api.upload_file(path_or_fileobj=file_path, path_in_repo=file, repo_id=repo_id)
    print("All sharded model files have been uploaded successfully!")

def upload_and_cleanup(temp_dir: str, oauth_token: gr.OAuthToken | None):
    # Handles the final upload process and cleans up the temporary directory.
    if not temp_dir or not os.path.exists(temp_dir):
        return "Error: No files found to upload.", "error.png", None, None, gr.update(visible=False), gr.update(visible=False)
    
    try:
        if oauth_token is None or oauth_token.token is None:
            raise gr.Error("Authentication token is missing. Please log in.")
        
        api = HfApi(token=oauth_token.token)
        username = whoami(token=oauth_token.token)["name"]

        quantized_gguf_path = next((os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith('.gguf')), None)
        imatrix_path = os.path.join(temp_dir, "imatrix.dat")
        readme_path = os.path.join(temp_dir, "README.md")
        private_repo_flag_path = os.path.join(temp_dir, "private_repo.flag")
        split_model_flag_path = os.path.join(temp_dir, "split_model.flag")
        split_tensors_path = os.path.join(temp_dir, "split_tensors.dat")
        split_size_path = os.path.join(temp_dir, "split_size.dat")

        if not quantized_gguf_path:
            raise FileNotFoundError("Could not find the quantized GGUF file.")

        quantized_gguf_name = os.path.basename(quantized_gguf_path)
        model_name = quantized_gguf_name.split('-')[0]
        quant_method_str = quantized_gguf_name.split('-')[1]

        is_private = os.path.exists(private_repo_flag_path)
        new_repo_id = f"{username}/{model_name}-{quant_method_str}-GGUF"
        new_repo_url = api.create_repo(repo_id=new_repo_id, exist_ok=True, private=is_private)
        print(f"Repo created/retrieved: {new_repo_url}")

        if os.path.exists(split_model_flag_path):
            max_tensors = int(open(split_tensors_path).read()) if os.path.exists(split_tensors_path) else 256
            max_size = open(split_size_path).read() if os.path.exists(split_size_path) else None
            split_and_upload_shards(quantized_gguf_path, temp_dir, new_repo_id, oauth_token.token, max_tensors, max_size)
        else:
            print(f"Uploading single file: {quantized_gguf_path}")
            api.upload_file(path_or_fileobj=quantized_gguf_path, path_in_repo=quantized_gguf_name, repo_id=new_repo_id)

        if os.path.exists(imatrix_path):
            api.upload_file(path_or_fileobj=imatrix_path, path_in_repo="imatrix.dat", repo_id=new_repo_id)
        if os.path.exists(readme_path):
            api.upload_file(path_or_fileobj=readme_path, path_in_repo="README.md", repo_id=new_repo_id)

        final_message = f'<h1>✅ UPLOAD COMPLETE</h1><br/>Find your repo here: <a href="{new_repo_url}" target="_blank" style="text-decoration:underline">{new_repo_id}</a>'
        final_image = "llama.png"

    except Exception as e:
        final_message = f'<h1>❌ UPLOAD ERROR</h1><br/><pre style="white-space:pre-wrap;">{escape_html(str(e))}</pre>'
        final_image = "error.png"
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
            print(f"Cleaned up temporary directory: {temp_dir}")

    return final_message, final_image, None, None, gr.update(visible=False), gr.update(visible=False)

def delete_files(temp_dir: str):
    # Deletes the temporary directory and resets the UI.
    if temp_dir and os.path.exists(temp_dir):
        shutil.rmtree(temp_dir)
        message = "Local files have been deleted."
        print(f"User deleted temporary directory: {temp_dir}")
    else:
        message = "No local files to delete."
    return message, "llama.png", None, None, gr.update(visible=False), gr.update(visible=False)

def process_model(model_id, q_method, use_imatrix, imatrix_q_method, private_repo, train_data_file, split_model, split_max_tensors, split_max_size, oauth_token: gr.OAuthToken | None):
    # Main function to download, convert, and quantize the model.
    
    # Unconditionally use the gr.OAuthToken object from the Login Button.
    if oauth_token is None or oauth_token.token is None:
        raise gr.Error("Authentication failed. Please log in to Hugging Face.")
    try:
        # Use the .token attribute directly
        whoami(token=oauth_token.token)
    except Exception as e:
        raise gr.Error(f"Authentication failed. Is your token valid? Error: {e}")

    model_name = model_id.split('/')[-1]
    
    # Ensure the outputs directory exists before trying to use it
    os.makedirs("outputs", exist_ok=True)
    
    outdir = tempfile.mkdtemp(dir="outputs")

    try:
        api = HfApi(token=oauth_token.token)
        dl_pattern = ["*.md", "*.json", "*.model"]
        try:
            repo_tree = api.list_repo_tree(repo_id=model_id, recursive=True)
            pattern = "*.safetensors" if any(f.path.endswith(".safetensors") for f in repo_tree) else "*.bin"
        except Exception:
            print("Could not determine primary file type, downloading both .safetensors and .bin")
            pattern = ["*.safetensors", "*.bin"]
        dl_pattern.extend(pattern if isinstance(pattern, list) else [pattern])

        if not os.path.exists("downloads"): os.makedirs("downloads")
        if not os.path.exists("outputs"): os.makedirs("outputs")

        fp16 = str(Path(outdir) / f"{model_name}.fp16.gguf")

        # --- START OF CACHING LOGIC ---
        # Define a permanent cache directory path
        model_cache_root = Path("./model_cache")
        # Sanitize the model_id to create a valid directory name (e.g., "google/gemma-2b" -> "google__gemma-2b")
        sanitized_model_id = model_id.replace("/", "__")
        local_dir = model_cache_root / sanitized_model_id

        # Check if the model is already cached by looking for a sentinel file
        sentinel_file = local_dir / ".download_complete"
        if local_dir.exists() and sentinel_file.exists():
            print(f"Model '{model_id}' found in cache. Skipping download.")
        else:
            print(f"Model '{model_id}' not found in cache. Starting download...")
            local_dir.mkdir(parents=True, exist_ok=True)
            api.snapshot_download(repo_id=model_id, local_dir=str(local_dir), local_dir_use_symlinks=False, allow_patterns=dl_pattern)
            # Create a sentinel file to mark the download as complete
            sentinel_file.touch()
            print("Download complete and cached.")
        # --- END OF CACHING LOGIC ---

        result = subprocess.run(["python", CONVERSION_SCRIPT, str(local_dir), "--outtype", "f16", "--outfile", fp16], capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Error converting to fp16: {result.stderr}")
        print(f"Model converted to fp16 successfully: {fp16}")

        imatrix_path = Path(outdir) / "imatrix.dat"
        if use_imatrix:
            train_data_path = train_data_file.name if train_data_file else "llama.cpp/groups_merged.txt"
            if not os.path.isfile(train_data_path):
                raise Exception(f"Training data file not found: {train_data_path}")
            generate_importance_matrix(fp16, train_data_path, str(imatrix_path))
        
        quant_method_str = (imatrix_q_method if use_imatrix else q_method).upper()
        quantized_gguf_name = f"{model_name.lower()}-{quant_method_str}.gguf"
        quantized_gguf_path = str(Path(outdir) / quantized_gguf_name)
        
        quantize_executable = get_platform_executable("llama-quantize")
        quantise_ggml = [quantize_executable]
        if use_imatrix:
            quantise_ggml.extend(["--imatrix", str(imatrix_path)])
        quantise_ggml.extend([fp16, quantized_gguf_path, quant_method_str])
        
        result = subprocess.run(quantise_ggml, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Error quantizing: {result.stderr}")
        print(f"Quantized successfully: {quantized_gguf_path}")

        if private_repo: open(os.path.join(outdir, "private_repo.flag"), 'a').close()
        if split_model:
            open(os.path.join(outdir, "split_model.flag"), 'a').close()
            with open(os.path.join(outdir, "split_tensors.dat"), 'w') as f: f.write(str(split_max_tensors))
            if split_max_size:
                with open(os.path.join(outdir, "split_size.dat"), 'w') as f: f.write(split_max_size)

        username = whoami(token=oauth_token.token)["name"]
        new_repo_id = f"{username}/{model_name}-{quant_method_str}-GGUF"
        space_id = os.environ.get("HF_SPACE_ID", "fentible/gguf-repo-suite")
        space_link = f"[{space_id.split('/')[-1]}](https://huggingface.co/spaces/{space_id})"
        card = ModelCard("")
        card.data.base_model = model_id
        card.text = f"# GGUF Model Card for {new_repo_id}\nConverted from [{model_id}](https://huggingface.co/{model_id}) via {space_link}."
        card.save(os.path.join(outdir, "README.md"))

        return (
            "Files generated successfully. You can now download them locally or choose an action below.",
            "llama.png",
            quantized_gguf_path,
            str(imatrix_path) if use_imatrix and os.path.exists(imatrix_path) else None,
            gr.update(visible=True),
            gr.update(visible=True),
            outdir,
        )
    except Exception as e:
        if os.path.exists(outdir): # Keep this commented out to prevent outputs folder from being automatically deleted
            shutil.rmtree(outdir) # Keep this commented out to prevent outputs folder from being automatically deleted
        return (
            f'<h1>❌ ERROR</h1><br/><pre style="white-space:pre-wrap;">{escape_html(str(e))}</pre>', # 1. output_markdown
            "error.png",                                                                    # 2. output_image
            None,                                                                           # 3. gguf_download_link
            None,                                                                           # 4. imatrix_download_link
            gr.update(visible=False),                                                       # 5. download_row
            gr.update(visible=False),                                                       # 6. action_row
            None                                                                            # 7. temp_dir_state
        )

# --- GRADIO UI DEFINITION ---

with gr.Blocks(css=".gradio-container {overflow-y: auto;}") as demo:
    gr.Markdown("# Create your own GGUF Quants, blazingly fast ⚡!")
    gr.Markdown(
        "The space takes an HF repo as an input, quantizes it and creates a Public repo containing the selected quant under your HF user namespace.\n\n"
        "This space (originally by ggml-org) was modified by Fentible to support lower IQ quants and local execution.\n\n"
        "See the readme here for more information: https://huggingface.co/spaces/Fentible/gguf-repo-suite/blob/main/README.md\n\n"
        "The 16GB CPU Basic version does not work on hugging face spaces. It hasn't been tested on a higher capacity rented space either.\n\n"
        "This modified suite is only confirmed to work on Windows. As such, you should clone this repo and host it locally via python venv."
    )

    # Create the Login Button, which will be visible in all environments.
    # Locally, it will use your cached hf_token. On a Space, it provides the full login flow.
    gr.Markdown("You must be logged in to upload to the Hub.")
    oauth_token_state = gr.LoginButton(min_width=250)

    gr.Markdown("## 1. Select Model and Quantization Options")
    with gr.Row():
        with gr.Column(scale=2):
            # Attempt to use the search component everywhere
            model_id = HuggingfaceHubSearch(
                label="Hub Model ID",
                placeholder="Search for model id on Huggingface",
                search_type="model",
            )
            with gr.Row():
                use_imatrix = gr.Checkbox(label="Use Imatrix Quantization", info="Use importance matrix for quantization.")
                private_repo = gr.Checkbox(label="Private Repo", info="Create a private repo under your username.")
                split_model = gr.Checkbox(label="Split Model", info="Shard the model using gguf-split.")
        with gr.Column(scale=1):
            q_method = gr.Dropdown(["TQ1_0", "TQ2_0", "Q2_K", "Q3_K_S", "Q3_K_M", "Q3_K_L", "Q4_0", "Q4_K_S", "Q4_K_M", "Q5_0", "Q5_K_S", "Q5_K_M", "Q6_K", "Q8_0"], label="Quantization Method", value="Q4_K_M", filterable=False)
            imatrix_q_method = gr.Dropdown(["IQ1_S", "IQ1_M", "IQ2_XXS", "IQ2_XS", "IQ2_S", "IQ2_M", "IQ3_XXS", "IQ3_XS", "IQ3_S", "IQ3_M", "Q4_K_M", "Q4_K_S", "IQ4_NL", "IQ4_XS", "Q5_K_M", "Q5_K_S"], label="Imatrix Quantization Method", value="IQ4_NL", filterable=False, visible=False)
            train_data_file = gr.File(label="Training Data File", visible=False)
            split_max_tensors = gr.Number(label="Max Tensors per File", value=256, visible=False)
            split_max_size = gr.Textbox(label="Max File Size", info="Accepted suffixes: M, G. Example: 256M, 5G", visible=False)

    quantize_btn = gr.Button("Quantize Model", variant="primary")

    gr.Markdown("## 2. Results")
    with gr.Row():
        output_markdown = gr.Markdown(label="Output")
        output_image = gr.Image(show_label=False, value="llama.png")

    with gr.Row(visible=False) as download_row:
        gguf_download_link = gr.File(label="Download Quantized GGUF", interactive=False)
        imatrix_download_link = gr.File(label="Download imatrix.dat", interactive=False, visible=False)

    with gr.Row(visible=False) as action_row:
        proceed_to_upload_btn = gr.Button("Proceed to Upload", variant="primary")
        delete_local_files_btn = gr.Button("Delete Local Files", variant="stop")

    temp_dir_state = gr.State()

    # --- Event Handlers ---
    quantize_btn.click(
        fn=process_model,
        inputs=[model_id, q_method, use_imatrix, imatrix_q_method, private_repo, train_data_file, split_model, split_max_tensors, split_max_size], # oauth_token_state NOW PASSED IMPLICITLY
        outputs=[output_markdown, output_image, gguf_download_link, imatrix_download_link, download_row, action_row, temp_dir_state]
    )
    proceed_to_upload_btn.click(
        fn=upload_and_cleanup,
        inputs=[temp_dir_state], # oauth_token_state NOW PASSED IMPLICITLY
        outputs=[output_markdown, output_image, gguf_download_link, imatrix_download_link, download_row, action_row]
    )
    delete_local_files_btn.click(
        fn=delete_files,
        inputs=[temp_dir_state],
        outputs=[output_markdown, output_image, gguf_download_link, imatrix_download_link, download_row, action_row]
    )
    split_model.change(lambda x: (gr.update(visible=x), gr.update(visible=x)), split_model, [split_max_tensors, split_max_size])
    use_imatrix.change(lambda x: (gr.update(visible=not x), gr.update(visible=x), gr.update(visible=x), gr.update(visible=x)), use_imatrix, [q_method, imatrix_q_method, train_data_file, imatrix_download_link])

# --- SCHEDULER & LAUNCH ---

space_id = os.environ.get("HF_SPACE_ID")
if space_id and HF_TOKEN:
    print(f"Running on HF Space: {space_id}. Scheduling a restart every 3 hours.")
    def restart_space():
        try:
            HfApi().restart_space(repo_id=space_id, token=HF_TOKEN, factory_reboot=True)
        except Exception as e:
            print(f"Error scheduling space restart: {e}")
    scheduler = BackgroundScheduler()
    scheduler.add_job(restart_space, "interval", seconds=10800)
    scheduler.start()
else:
    print("Not running on a Hugging Face Space or HF_TOKEN not set. Skipping space restart schedule.")

demo.queue(default_concurrency_limit=1, max_size=5).launch(debug=True, show_api=False)