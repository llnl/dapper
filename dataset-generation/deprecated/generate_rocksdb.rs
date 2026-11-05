use rocksdb::{Options, DB};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::fs::File;
use std::io::{self, prelude::*, BufReader};

#[derive(Serialize, Deserialize, Debug)]
struct MyData {
    items: Vec<(String, String)>, // A vector of tuples
}

// NB: db is automatically closed at end of lifetime

fn main() {
    // open the file, read it
    let mut file =
        File::open("insert/path/here")
            .unwrap();
    let mut reader = BufReader::new(file);
    let lines: Vec<String> = reader.lines().collect::<Result<_, _>>().unwrap();

    // initialize filename to empty string
    let mut filename: &str = "";

    // initialize the database
    let path = "./rocksdb";
    let db = DB::open_default(path).unwrap();

    // iterate over the lines in the file and pull out the information we want in the database
    for (index, element) in lines.into_iter().enumerate() {
        // do some processing on the string extracted to remove whitespace, tabs, newlines
        if let Some((full_path, pkg)) = element.rsplit_once([' ', '\t']) {
            // edit the full_path to get the file name
            let filename = full_path.trim_end().rsplit('/').next().unwrap().to_string();

            // package the information into a tuple of strings
            let pkg_filepath = (pkg, full_path);

            // serialize the data
            let mut my_data = MyData { items: Vec::new() };
            my_data
                .items
                .push((pkg_filepath.0.to_string(), pkg_filepath.1.to_string()));
            let json_value = json!(my_data);
            let value_bytes = json_value.to_string().into_bytes();

            // if the key already exists
            if let Ok(Some(value)) = db.get(&filename)
            {
                // deserialize the result that was previously stored
                
                let deserialized_bytes = String::from_utf8(value);

                let mut my_data: MyData =
                    match serde_json::from_str(&deserialized_bytes.expect("A string")) {
                        Ok(data) => data,
                        Err(e) => {
                            eprintln!("Failed to deserialize data: {}", e);
                            return;
                        }
                    };

                // Append a new tuple to the items vector
                my_data
                    .items
                    .push((pkg_filepath.0.to_string(), pkg_filepath.1.to_string()));

                // Serialize the updated data back to JSON
                let serialized_data = json!(my_data);
                let serialized_value_bytes = serialized_data.to_string().into_bytes();

                // Put the updated value back into the database
                if let Err(e) = db.put(&filename, serialized_value_bytes) {
                    eprintln!("Failed to put updated data in database: {}", e);
                } else {
                    // println!("Successfully updated the data for key {:?}", filename);
                }

            } else {
                // add a new entry to the db
                let _ = db.put(&filename, value_bytes);
            }

        }
    }
 
}
