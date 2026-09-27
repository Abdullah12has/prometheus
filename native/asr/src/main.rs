#[cfg(target_os = "macos")]
mod bridge {
    use serde_json::{json, Value};
    use std::{
        io::{self, BufRead, Write},
        path::Path,
        sync::mpsc,
        thread,
    };
    use transcribe_cpp::{Backend, Model, ModelOptions, RunOptions, Session, StreamOptions};

    const MAX_FEED_SAMPLES: usize = 160_000;
    const MAX_WAV_SAMPLES: usize = 9_600_000;

    enum Command {
        Start(String),
        Feed(String, Vec<f32>),
        Finalize(String),
        Cancel(String),
        Transcribe(String, String),
    }

    fn valid_id(v: &Value) -> Result<String, String> {
        let id = v
            .get("id")
            .and_then(Value::as_str)
            .ok_or("id must be a non-empty string")?;
        if id.trim().is_empty() || id.len() > 128 {
            return Err("id must contain 1–128 characters".into());
        }
        Ok(id.to_owned())
    }

    fn parse_command(v: Value) -> Result<Command, (Option<String>, String)> {
        let id = valid_id(&v).map_err(|e| (None, e))?;
        let op = v
            .get("op")
            .and_then(Value::as_str)
            .ok_or_else(|| (Some(id.clone()), "op must be a string".into()))?;
        let cmd = match op {
            "start" => Command::Start(id),
            "feed" => {
                let xs = v
                    .get("samples")
                    .and_then(Value::as_array)
                    .ok_or_else(|| (Some(id.clone()), "samples must be an array".into()))?;
                if xs.is_empty() || xs.len() > MAX_FEED_SAMPLES {
                    return Err((
                        Some(id),
                        format!("feed must contain 1–{MAX_FEED_SAMPLES} samples"),
                    ));
                }
                let mut samples = Vec::with_capacity(xs.len());
                for x in xs {
                    let n = x.as_f64().ok_or_else(|| {
                        (Some(id.clone()), "samples must be finite numbers".into())
                    })?;
                    if !n.is_finite() || !(-1.0..=1.0).contains(&n) {
                        return Err((Some(id), "samples must be finite values in [-1, 1]".into()));
                    }
                    samples.push(n as f32);
                }
                Command::Feed(id, samples)
            }
            "finalize" => Command::Finalize(id),
            "cancel" => Command::Cancel(id),
            "transcribe" => {
                let path = v
                    .get("path")
                    .and_then(Value::as_str)
                    .filter(|s| !s.trim().is_empty())
                    .ok_or_else(|| (Some(id.clone()), "path must be a non-empty string".into()))?;
                Command::Transcribe(id, path.to_owned())
            }
            _ => return Err((Some(id), "unsupported op".into())),
        };
        Ok(cmd)
    }

    fn error(id: Option<&str>, message: impl AsRef<str>) -> Value {
        json!({"id": id, "type": "error", "error": message.as_ref()})
    }

    fn wav_samples(path: &Path) -> Result<Vec<f32>, String> {
        let mut reader = hound::WavReader::open(path).map_err(|e| e.to_string())?;
        let spec = reader.spec();
        if spec.channels != 1 || spec.sample_rate != 16_000 {
            return Err("WAV must be 16 kHz mono PCM".into());
        }
        if reader.duration() == 0 || reader.duration() as usize > MAX_WAV_SAMPLES {
            return Err(format!("WAV must contain 1–{MAX_WAV_SAMPLES} samples"));
        }
        let samples = match spec.sample_format {
            hound::SampleFormat::Int if spec.bits_per_sample == 16 => reader
                .samples::<i16>()
                .map(|s| s.map(|v| v as f32 / 32768.0))
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?,
            hound::SampleFormat::Float if spec.bits_per_sample == 32 => reader
                .samples::<f32>()
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?,
            _ => return Err("WAV must use 16-bit PCM or 32-bit float samples".into()),
        };
        if samples.is_empty() || samples.len() > MAX_WAV_SAMPLES {
            return Err(format!("WAV must contain 1–{MAX_WAV_SAMPLES} samples"));
        }
        if samples
            .iter()
            .any(|x| !x.is_finite() || !(-1.0..=1.0).contains(x))
        {
            return Err("WAV samples must be finite values in [-1, 1]".into());
        }
        Ok(samples)
    }

    fn handle(session: &mut Session, rx: &mpsc::Receiver<(Command, mpsc::Sender<Value>)>) {
        while let Ok((command, reply)) = rx.recv() {
            match command {
                Command::Start(id) => {
                    let opts = RunOptions::default();
                    match session.stream(&opts, &StreamOptions::default()) {
                        Err(e) => {
                            let _ = reply.send(error(Some(&id), e.to_string()));
                        }
                        Ok(mut stream) => {
                            let _ = reply.send(json!({"id": id, "type": "started"}));
                            loop {
                                let Ok((cmd, reply)) = rx.recv() else { return };
                                let response = match cmd {
                                    Command::Feed(cmd_id, samples) if cmd_id == id => {
                                        match stream.feed(&samples) {
                                            Ok(_) => {
                                                let text = stream.text();
                                                json!({"id": id, "type": "partial", "committed": text.committed, "tentative": text.tentative})
                                            }
                                            Err(e) => error(Some(&id), e.to_string()),
                                        }
                                    }
                                    Command::Finalize(cmd_id) if cmd_id == id => {
                                        match stream.finalize() {
                                            Ok(_) => {
                                                let text = stream.text().display();
                                                let _ = reply.send(json!({"id": id, "type": "final", "text": text}));
                                                break;
                                            }
                                            Err(e) => {
                                                let _ = reply.send(error(Some(&id), e.to_string()));
                                                break;
                                            }
                                        }
                                    }
                                    Command::Cancel(cmd_id) if cmd_id == id => {
                                        stream.reset();
                                        let _ = reply.send(json!({"id": id, "type": "cancelled"}));
                                        break;
                                    }
                                    Command::Feed(cmd_id, _)
                                    | Command::Finalize(cmd_id)
                                    | Command::Cancel(cmd_id) => {
                                        error(Some(&cmd_id), "active stream requires matching id")
                                    }
                                    Command::Start(cmd_id) | Command::Transcribe(cmd_id, _) => {
                                        error(
                                            Some(&cmd_id),
                                            "finish or cancel the active stream first",
                                        )
                                    }
                                };
                                let _ = reply.send(response);
                            }
                        }
                    }
                }
                Command::Transcribe(id, path) => {
                    let result = wav_samples(Path::new(&path)).and_then(|samples| {
                        session
                            .run(&samples, &RunOptions::default())
                            .map(|r| r.text)
                            .map_err(|e| e.to_string())
                    });
                    let value = match result {
                        Ok(text) => json!({"id": id, "type": "final", "text": text}),
                        Err(e) => error(Some(&id), e),
                    };
                    let _ = reply.send(value);
                }
                Command::Feed(id, _) | Command::Finalize(id) | Command::Cancel(id) => {
                    let _ = reply.send(error(Some(&id), "no active stream"));
                }
            }
        }
    }

    fn write_json(out: &mut impl Write, value: &Value) -> io::Result<()> {
        serde_json::to_writer(&mut *out, value)?;
        out.write_all(b"\n")?;
        out.flush()
    }

    pub fn run() -> Result<(), String> {
        let mut args = std::env::args().skip(1);
        if args.next().as_deref() != Some("--model") {
            return Err("usage: native-asr-bridge --model /path/to/model.gguf".into());
        }
        let path = args.next().ok_or("missing model path")?;
        if args.next().is_some() {
            return Err("unexpected arguments".into());
        }
        let model = Model::load_with(
            &path,
            &ModelOptions {
                backend: Backend::Metal,
                gpu_device: 0,
            },
        )
        .map_err(|e| format!("model load failed: {e}"))?;
        let mut session = model
            .session()
            .map_err(|e| format!("session creation failed: {e}"))?;
        let (tx, rx) = mpsc::channel::<(Command, mpsc::Sender<Value>)>();
        let worker = thread::spawn(move || handle(&mut session, &rx));
        let stdin = io::stdin();
        let mut stdout = io::BufWriter::new(io::stdout().lock());
        write_json(&mut stdout, &json!({"type": "ready"})).map_err(|e| e.to_string())?;
        for line in stdin.lock().lines() {
            let line = match line {
                Ok(s) => s,
                Err(e) => {
                    eprintln!("stdin read: {e}");
                    break;
                }
            };
            let parsed = serde_json::from_str::<Value>(&line);
            let command = match parsed {
                Ok(v) => parse_command(v),
                Err(e) => Err((None, format!("invalid JSON: {e}"))),
            };
            match command {
                Err((id, message)) => write_json(&mut stdout, &error(id.as_deref(), message))
                    .map_err(|e| e.to_string())?,
                Ok(command) => {
                    let (reply_tx, reply_rx) = mpsc::channel();
                    if tx.send((command, reply_tx)).is_err() {
                        return Err("ASR worker stopped".into());
                    }
                    match reply_rx.recv() {
                        Ok(value) => write_json(&mut stdout, &value).map_err(|e| e.to_string())?,
                        Err(_) => return Err("ASR worker stopped before replying".into()),
                    }
                }
            }
        }
        drop(tx);
        worker
            .join()
            .map_err(|_| "ASR worker panicked".to_string())?;
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn rejects_non_finite_or_out_of_range_feed() {
            for value in [f64::NAN, f64::INFINITY, 1.01, -1.01] {
                let input = json!({"id":"x","op":"feed","samples":[value]});
                assert!(parse_command(input).is_err());
            }
        }
        #[test]
        fn parses_valid_start_and_feed() {
            assert!(
                matches!(parse_command(json!({"id":"s","op":"start"})), Ok(Command::Start(id)) if id == "s")
            );
            assert!(
                matches!(parse_command(json!({"id":"s","op":"feed","samples":[0.0,0.5]})), Ok(Command::Feed(id, samples)) if id == "s" && samples.len() == 2)
            );
        }
    }
}

#[cfg(target_os = "macos")]
fn main() {
    if let Err(message) = bridge::run() {
        eprintln!("{message}");
        std::process::exit(2);
    }
}

#[cfg(not(target_os = "macos"))]
fn main() {
    eprintln!("native-asr-bridge requires macOS with Metal support");
    std::process::exit(2);
}
