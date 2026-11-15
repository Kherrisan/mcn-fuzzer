//! Precomputed PCGUARD map-index to source-location tables.
//!
//! A PCGUARD build with `-fsanitize-coverage=...,pc-table` emits two parallel
//! arrays: `__sancov_guards` (one `u32` guard per instrumented block) and
//! `__sancov_pcs` (one `{PC, flags}` entry per block, same order). At startup
//! AFL++ assigns guard `i` the map index `GUARD_ID_BASE + i`, so the two tables
//! already give us index -> PC without running the target.
//!
//! Symbolizing those PCs once per build yields a stable map. Both directions
//! are stored compressed: `index -> location` and `location -> indexes`. The
//! reverse direction is one-to-many because optimization and inlining can place
//! several instrumented blocks on one source line.

use std::{
    collections::BTreeMap,
    fs,
    io::{Read, Write},
    path::Path,
    process::{Command, Stdio},
};

use flate2::{read::ZlibDecoder, write::ZlibEncoder, Compression};
use object::{Object, ObjectSection};

/// First map index AFL++ assigns in `__sanitizer_cov_trace_pc_guard_init`.
pub const GUARD_ID_BASE: u32 = 5;
const MAGIC: &[u8; 8] = b"MCNPGMAP";
const VERSION: u32 = 1;

/// One symbolized source position.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Frame {
    pub function: String,
    pub file: String,
    pub line: u32,
}

/// Both directions of the precomputed mapping.
#[derive(Clone, Debug, Default)]
pub struct SymbolMap {
    /// Map index -> innermost frame of that instrumented block.
    pub index: BTreeMap<u32, Frame>,
    /// `(file, line)` -> every index whose inline chain reaches that line.
    pub locations: BTreeMap<(String, u32), Vec<u32>>,
}

impl SymbolMap {
    /// Build the maps from a PCGUARD binary that carries the pc-table.
    pub fn build(binary: &Path, symbolizer: &str) -> Result<Self, String> {
        let data =
            fs::read(binary).map_err(|error| format!("reading {}: {error}", binary.display()))?;
        let file = object::File::parse(&*data)
            .map_err(|error| format!("parsing {}: {error}", binary.display()))?;
        let guards = section(&file, "__sancov_guards")?;
        let pcs = section(&file, "__sancov_pcs")?;
        let count = (guards.len() / 4).min(pcs.len() / 16);
        if count == 0 {
            return Err(format!(
                "{} has no pcguard tables; rebuild it with \
                 AFL_LLVM_INSTRUMENT=LLVMNATIVE so `__sancov_guards` and \
                 `__sancov_pcs` are emitted",
                binary.display()
            ));
        }
        let mut addresses = Vec::with_capacity(count);
        for index in 0..count {
            let start = index * 16;
            let pc = u64::from_le_bytes(
                pcs[start..start + 8]
                    .try_into()
                    .map_err(|_| "short __sancov_pcs entry".to_string())?,
            );
            addresses.push(pc);
        }
        let frames = symbolize(symbolizer, binary, &addresses)?;
        let mut map = SymbolMap::default();
        for (offset, chain) in frames.into_iter().enumerate() {
            let id = GUARD_ID_BASE + offset as u32;
            // The outermost frame names the function that actually contains the
            // block; inner frames are inlined callees. Keep every frame in the
            // reverse table so an inlined line still resolves to this index.
            if let Some(primary) = chain.last() {
                map.index.insert(id, primary.clone());
            }
            for frame in chain {
                if frame.file.is_empty() || frame.line == 0 {
                    continue;
                }
                map.locations
                    .entry((frame.file, frame.line))
                    .or_default()
                    .push(id);
            }
        }
        for ids in map.locations.values_mut() {
            ids.sort_unstable();
            ids.dedup();
        }
        Ok(map)
    }

    /// Compress and write both tables.
    pub fn write(&self, index_path: &Path, location_path: &Path) -> Result<(), String> {
        fs::write(index_path, self.encode_index()?)
            .map_err(|error| format!("writing {}: {error}", index_path.display()))?;
        fs::write(location_path, self.encode_locations()?)
            .map_err(|error| format!("writing {}: {error}", location_path.display()))?;
        Ok(())
    }

    /// Read both tables back.
    pub fn read(index_path: &Path, location_path: &Path) -> Result<Self, String> {
        let index = decode_index(index_path)?;
        let locations = decode_locations(location_path)?;
        Ok(Self { index, locations })
    }

    fn encode_index(&self) -> Result<Vec<u8>, String> {
        let mut strings = StringTable::default();
        let mut records = Vec::with_capacity(self.index.len());
        for (id, frame) in &self.index {
            records.push((
                *id,
                strings.id(&frame.file),
                strings.id(&frame.function),
                frame.line,
            ));
        }
        let mut raw = Vec::new();
        raw.extend_from_slice(MAGIC);
        raw.extend_from_slice(&VERSION.to_le_bytes());
        strings.write(&mut raw);
        raw.extend_from_slice(&varint(records.len() as u64));
        let mut previous = 0u32;
        for (id, file, function, line) in records {
            raw.extend_from_slice(&varint(u64::from(id - previous)));
            previous = id;
            raw.extend_from_slice(&varint(u64::from(file)));
            raw.extend_from_slice(&varint(u64::from(line)));
            raw.extend_from_slice(&varint(u64::from(function)));
        }
        Ok(zlib(&raw))
    }

    fn encode_locations(&self) -> Result<Vec<u8>, String> {
        let mut strings = StringTable::default();
        let mut records = Vec::with_capacity(self.locations.len());
        for ((file, line), ids) in &self.locations {
            records.push((strings.id(file), *line, ids));
        }
        let mut raw = Vec::new();
        raw.extend_from_slice(MAGIC);
        raw.extend_from_slice(&VERSION.to_le_bytes());
        strings.write(&mut raw);
        raw.extend_from_slice(&varint(records.len() as u64));
        let (mut previous_file, mut previous_line) = (0u32, 0u32);
        for (file, line, ids) in records {
            raw.extend_from_slice(&varint(u64::from(file.wrapping_sub(previous_file))));
            previous_file = file;
            raw.extend_from_slice(&varint(u64::from(line.wrapping_sub(previous_line))));
            previous_line = line;
            raw.extend_from_slice(&varint(ids.len() as u64));
            let mut previous_id = 0u32;
            for id in ids {
                raw.extend_from_slice(&varint(u64::from(id.wrapping_sub(previous_id))));
                previous_id = *id;
            }
        }
        Ok(zlib(&raw))
    }
}

fn section<'a>(file: &'a object::File<'a>, name: &str) -> Result<&'a [u8], String> {
    let section = file
        .section_by_name(name)
        .ok_or_else(|| format!("binary has no {name} section"))?;
    section
        .data()
        .map_err(|error| format!("reading {name}: {error}"))
}

/// Symbolize addresses in one `llvm-symbolizer` call and return each inline chain.
fn symbolize(
    symbolizer: &str,
    binary: &Path,
    addresses: &[u64],
) -> Result<Vec<Vec<Frame>>, String> {
    let mut child = Command::new(symbolizer)
        .arg(format!("--obj={}", binary.display()))
        .arg("--functions")
        .arg("--inlines")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("starting {symbolizer}: {error}"))?;
    // Write addresses and drain output concurrently: a single-threaded
    // write-then-read deadlocks once either pipe buffer fills.
    let mut stdin = child.stdin.take().ok_or("symbolizer stdin unavailable")?;
    let mut stdout = child.stdout.take().ok_or("symbolizer stdout unavailable")?;
    let mut stderr = child.stderr.take();
    let mut text = String::with_capacity(addresses.len() * 12);
    for address in addresses {
        text.push_str(&format!("0x{address:x}\n"));
    }
    let writer = std::thread::spawn(move || {
        let _ = stdin.write_all(text.as_bytes());
        let _ = stdin.flush();
    });
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        let _ = stdout.read_to_string(&mut text);
        text
    });
    let errors = std::thread::spawn(move || {
        let mut text = String::new();
        if let Some(mut stderr) = stderr.take() {
            let _ = stderr.read_to_string(&mut text);
        }
        text
    });
    let status = child
        .wait()
        .map_err(|error| format!("waiting for {symbolizer}: {error}"))?;
    let _ = writer.join();
    let output = reader.join().unwrap_or_default();
    let errors = errors.join().unwrap_or_default();
    if !status.success() {
        return Err(format!("{symbolizer} failed: {}", errors.trim()));
    }
    Ok(parse_symbolizer(&output))
}

fn parse_symbolizer(text: &str) -> Vec<Vec<Frame>> {
    let mut chains = Vec::new();
    for block in text.split("\n\n") {
        let block = block.trim_matches('\n');
        if block.is_empty() {
            continue;
        }
        let lines: Vec<&str> = block.lines().collect();
        let mut chain = Vec::new();
        let mut index = 0;
        while index + 1 < lines.len() {
            let function = lines[index].trim();
            let location = lines[index + 1].trim();
            index += 2;
            let Some((file, line)) = parse_location(location) else {
                continue;
            };
            chain.push(Frame {
                function: if function == "??" {
                    String::new()
                } else {
                    function.to_string()
                },
                file,
                line,
            });
        }
        chains.push(chain);
    }
    chains
}

fn parse_location(text: &str) -> Option<(String, u32)> {
    let mut parts = text.rsplitn(3, ':');
    let _column = parts.next()?;
    let line: u32 = parts.next()?.parse().ok()?;
    let file = parts.next()?.to_string();
    if file.is_empty() || file == "??" || line == 0 {
        return None;
    }
    Some((file, line))
}

fn decode_index(path: &Path) -> Result<BTreeMap<u32, Frame>, String> {
    let mut raw = unzlib(path)?;
    let mut cursor = Cursor::new(&mut raw);
    cursor.header()?;
    let strings = StringTable::read(&mut cursor)?;
    let count = cursor.varint()? as usize;
    let mut map = BTreeMap::new();
    let mut id = 0u32;
    for _ in 0..count {
        id = id.wrapping_add(cursor.varint()? as u32);
        let file = strings.get(cursor.varint()? as usize)?;
        let line = cursor.varint()? as u32;
        let function = strings.get(cursor.varint()? as usize)?;
        map.insert(
            id,
            Frame {
                function,
                file,
                line,
            },
        );
    }
    Ok(map)
}

fn decode_locations(path: &Path) -> Result<BTreeMap<(String, u32), Vec<u32>>, String> {
    let mut raw = unzlib(path)?;
    let mut cursor = Cursor::new(&mut raw);
    cursor.header()?;
    let strings = StringTable::read(&mut cursor)?;
    let count = cursor.varint()? as usize;
    let mut map = BTreeMap::new();
    let (mut file, mut line) = (0u32, 0u32);
    for _ in 0..count {
        file = file.wrapping_add(cursor.varint()? as u32);
        line = line.wrapping_add(cursor.varint()? as u32);
        let ids = cursor.varint()? as usize;
        let mut values = Vec::with_capacity(ids);
        let mut id = 0u32;
        for _ in 0..ids {
            id = id.wrapping_add(cursor.varint()? as u32);
            values.push(id);
        }
        map.insert((strings.get(file as usize)?, line), values);
    }
    Ok(map)
}

fn zlib(raw: &[u8]) -> Vec<u8> {
    let mut encoder = ZlibEncoder::new(Vec::new(), Compression::best());
    let _ = encoder.write_all(raw);
    encoder.finish().unwrap_or_default()
}

fn unzlib(path: &Path) -> Result<Vec<u8>, String> {
    let mut decoder = ZlibDecoder::new(
        fs::File::open(path).map_err(|error| format!("opening {}: {error}", path.display()))?,
    );
    let mut raw = Vec::new();
    decoder
        .read_to_end(&mut raw)
        .map_err(|error| format!("decompressing {}: {error}", path.display()))?;
    Ok(raw)
}

fn varint(value: u64) -> Vec<u8> {
    let mut out = Vec::with_capacity(10);
    let mut value = value;
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        if value == 0 {
            out.push(byte);
            return out;
        }
        out.push(byte | 0x80);
    }
}

#[derive(Default)]
struct StringTable {
    ids: BTreeMap<String, u32>,
    values: Vec<String>,
}

impl StringTable {
    fn id(&mut self, value: &str) -> u32 {
        if let Some(id) = self.ids.get(value) {
            return *id;
        }
        let id = self.values.len() as u32;
        self.values.push(value.to_string());
        self.ids.insert(value.to_string(), id);
        id
    }

    fn get(&self, id: usize) -> Result<String, String> {
        self.values
            .get(id)
            .cloned()
            .ok_or_else(|| format!("string id {id} is out of range"))
    }

    fn write(&self, out: &mut Vec<u8>) {
        out.extend_from_slice(&varint(self.values.len() as u64));
        for value in &self.values {
            out.extend_from_slice(&varint(value.len() as u64));
            out.extend_from_slice(value.as_bytes());
        }
    }

    fn read(cursor: &mut Cursor<'_>) -> Result<Self, String> {
        let count = cursor.varint()? as usize;
        let mut values = Vec::with_capacity(count);
        let mut ids = BTreeMap::new();
        for id in 0..count {
            let len = cursor.varint()? as usize;
            let value = cursor.bytes(len)?;
            let text = std::str::from_utf8(value)
                .map_err(|error| error.to_string())?
                .to_string();
            ids.insert(text.clone(), id as u32);
            values.push(text);
        }
        Ok(Self { ids, values })
    }
}

struct Cursor<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    fn header(&mut self) -> Result<(), String> {
        if self.data.len() < 12 {
            return Err("symbol map is truncated".into());
        }
        if &self.data[..8] != MAGIC {
            return Err("symbol map has a wrong magic".into());
        }
        let version = u32::from_le_bytes(self.data[8..12].try_into().unwrap());
        if version != VERSION {
            return Err(format!("symbol map version {version} is not supported"));
        }
        self.offset = 12;
        Ok(())
    }

    fn varint(&mut self) -> Result<u64, String> {
        let mut result = 0u64;
        let mut shift = 0;
        loop {
            let byte = *self
                .data
                .get(self.offset)
                .ok_or_else(|| "symbol map varint is truncated".to_string())?;
            self.offset += 1;
            result |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Ok(result);
            }
            shift += 7;
            if shift >= 64 {
                return Err("symbol map varint overflows".into());
            }
        }
    }

    fn bytes(&mut self, len: usize) -> Result<&'a [u8], String> {
        let end = self
            .offset
            .checked_add(len)
            .filter(|end| *end <= self.data.len())
            .ok_or_else(|| "symbol map string is truncated".to_string())?;
        let slice = &self.data[self.offset..end];
        self.offset = end;
        Ok(slice)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample() -> SymbolMap {
        let mut map = SymbolMap::default();
        map.index.insert(
            5,
            Frame {
                function: "main".into(),
                file: "/src/a.c".into(),
                line: 10,
            },
        );
        map.index.insert(
            6,
            Frame {
                function: "helper".into(),
                file: "/src/b.c".into(),
                line: 3,
            },
        );
        map.locations.insert(("/src/a.c".into(), 10), vec![5, 7]);
        map.locations.insert(("/src/b.c".into(), 3), vec![6]);
        map
    }

    #[test]
    fn varint_roundtrips() {
        for value in [0u64, 1, 127, 128, 300, 1 << 20, u64::from(u32::MAX)] {
            let encoded = varint(value);
            let mut data = MAGIC.to_vec();
            data.extend_from_slice(&VERSION.to_le_bytes());
            data.extend_from_slice(&encoded);
            let mut cursor = Cursor::new(&data);
            cursor.header().unwrap();
            assert_eq!(cursor.varint().unwrap(), value);
        }
    }

    #[test]
    fn both_tables_roundtrip() {
        let dir = std::env::temp_dir().join(format!("mcn-symbol-map-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let index = dir.join("index.z");
        let locations = dir.join("locations.z");
        let map = sample();
        map.write(&index, &locations).unwrap();
        let read = SymbolMap::read(&index, &locations).unwrap();
        assert_eq!(read.index, map.index);
        assert_eq!(read.locations, map.locations);
        assert!(fs::metadata(&index).unwrap().len() < 4096);
        let _ = fs::remove_dir_all(&dir);
    }
}

/// `mcn-fuzz symbol-map`: build the tables or query them.
pub fn command(
    binary: Option<&Path>,
    index_map: Option<&Path>,
    location_map: Option<&Path>,
    symbolizer: &str,
    lookup_index: Option<u32>,
    lookup_location: Option<&str>,
) -> Result<(), String> {
    if lookup_index.is_some() || lookup_location.is_some() {
        let (Some(index_path), Some(location_path)) = (index_map, location_map) else {
            return Err("--lookup-* needs --index-map and --location-map".into());
        };
        let map = SymbolMap::read(index_path, location_path)?;
        if let Some(id) = lookup_index {
            match map.index.get(&id) {
                Some(frame) => println!(
                    "{id}\t{}\t{}:{}",
                    if frame.function.is_empty() {
                        "??"
                    } else {
                        &frame.function
                    },
                    frame.file,
                    frame.line
                ),
                None => println!("{id}\t(no instrumented block with this map index)"),
            }
        }
        if let Some(spec) = lookup_location {
            let Some((file, line)) = spec.rsplit_once(':') else {
                return Err("--lookup-location must be file:line".into());
            };
            let line: u32 = line
                .parse()
                .map_err(|_| "--lookup-location line is not a number".to_string())?;
            let suffix = format!("/{file}");
            let mut matches = Vec::new();
            for ((candidate, candidate_line), ids) in &map.locations {
                if *candidate_line != line {
                    continue;
                }
                if candidate == file || candidate.ends_with(&suffix) {
                    matches.extend(ids.iter().copied());
                }
            }
            matches.sort_unstable();
            matches.dedup();
            if matches.is_empty() {
                println!("{file}:{line}\t(no index reaches this line)");
            } else {
                let joined = matches
                    .iter()
                    .map(u32::to_string)
                    .collect::<Vec<_>>()
                    .join(",");
                println!("{file}:{line}\t{joined}");
            }
        }
        return Ok(());
    }

    let Some(binary) = binary else {
        return Err("symbol-map needs --binary (with --index-map and --location-map)".into());
    };
    let (Some(index_path), Some(location_path)) = (index_map, location_map) else {
        return Err("symbol-map needs --index-map and --location-map".into());
    };
    let map = SymbolMap::build(binary, symbolizer)?;
    if let Some(parent) = index_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("creating {}: {error}", parent.display()))?;
    }
    if let Some(parent) = location_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("creating {}: {error}", parent.display()))?;
    }
    map.write(index_path, location_path)?;
    let index_bytes = fs::metadata(index_path).map(|m| m.len()).unwrap_or(0);
    let location_bytes = fs::metadata(location_path).map(|m| m.len()).unwrap_or(0);
    println!(
        "symbol map: {} indexes, {} locations",
        map.index.len(),
        map.locations.len()
    );
    println!("  {}  {index_bytes} bytes", index_path.display());
    println!("  {}  {location_bytes} bytes", location_path.display());
    Ok(())
}
